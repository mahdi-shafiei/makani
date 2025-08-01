# SPDX-FileCopyrightText: Copyright (c) 2024 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
# http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import torch
import torch.nn as nn
import torch.nn.functional as F
import numpy as np
import math

from torch import amp

# import convenience functions for factorized tensors
from makani.utils import comm
from makani.models.common import ComplexReLU
from makani.models.common.contractions import _contract_rank
from makani.models.common.factorizations import get_contract_fun

import torch_harmonics as th
import torch_harmonics.distributed as thd


class SpectralConv(nn.Module):
    """
    Spectral Convolution implemented via SHT or FFT. Designed for convolutions on the two-sphere S2
    using the Spherical Harmonic Transforms in torch-harmonics, but supports convolutions on the periodic
    domain via the RealFFT2 and InverseRealFFT2 wrappers.
    """

    def __init__(self, forward_transform, inverse_transform, in_channels, out_channels, num_groups=1, operator_type="dhconv", separable=False, bias=False, gain=1.0):
        super().__init__()

        assert in_channels % num_groups == 0
        assert out_channels % num_groups == 0

        self.forward_transform = forward_transform
        self.inverse_transform = inverse_transform

        self.in_channels = in_channels
        self.out_channels = out_channels
        self.num_groups = num_groups

        self.modes_lat = self.inverse_transform.lmax
        self.modes_lon = self.inverse_transform.mmax

        self.scale_residual = (self.forward_transform.nlat != self.inverse_transform.nlat) or (self.forward_transform.nlon != self.inverse_transform.nlon)
        if hasattr(self.forward_transform, "grid"):
            self.scale_residual = self.scale_residual or (self.forward_transform.grid != self.inverse_transform.grid)

        # remember factorization details
        self.operator_type = operator_type
        self.separable = separable

        assert self.inverse_transform.lmax == self.modes_lat
        assert self.inverse_transform.mmax == self.modes_lon

        weight_shape = [num_groups, in_channels // num_groups]

        if not self.separable:
            weight_shape += [out_channels // num_groups]

        if isinstance(self.inverse_transform, thd.DistributedInverseRealSHT):
            self.modes_lat_local = self.inverse_transform.l_shapes[comm.get_rank("h")]
            self.modes_lon_local = self.inverse_transform.m_shapes[comm.get_rank("w")]
            self.nlat_local = self.inverse_transform.lat_shapes[comm.get_rank("h")]
            self.nlon_local = self.inverse_transform.lon_shapes[comm.get_rank("w")]
        else:
            self.modes_lat_local = self.modes_lat
            self.modes_lon_local = self.modes_lon
            self.nlat_local = self.inverse_transform.nlat
            self.nlon_local = self.inverse_transform.nlon

        # unpadded weights
        if self.operator_type == "diagonal":
            weight_shape += [self.modes_lat_local, self.modes_lon_local]
        elif self.operator_type == "dhconv":
            weight_shape += [self.modes_lat_local]
        else:
            raise ValueError(f"Unsupported operator type f{self.operator_type}")

        # Compute scaling factor for correct initialization
        scale = math.sqrt(gain / (in_channels // num_groups)) * torch.ones(self.modes_lat_local, dtype=torch.complex64)
        # seemingly the first weight is not really complex, so we need to account for that
        scale[0] *= math.sqrt(2.0)
        init = scale * torch.randn(*weight_shape, dtype=torch.complex64)
        self.weight = nn.Parameter(init)

        if self.operator_type == "dhconv":
            self.weight.is_shared_mp = ["matmul", "w"]
            self.weight.sharded_dims_mp = [None for _ in weight_shape]
            self.weight.sharded_dims_mp[-1] = "h"
        else:
            self.weight.is_shared_mp = ["matmul"]
            self.weight.sharded_dims_mp = [None for _ in weight_shape]
            self.weight.sharded_dims_mp[-1] = "w"
            self.weight.sharded_dims_mp[-2] = "h"

        # get the contraction handle. This should return a pyTorch contraction
        self._contract = get_contract_fun(self.weight, implementation="factorized", separable=separable, complex=True, operator_type=operator_type)

        if bias == True:
            self.bias = nn.Parameter(torch.zeros(1, self.out_channels, 1, 1))
            self.bias.is_shared_mp = ["model"]
            self.bias.sharded_dims_mp = [None, None, None, None]

    def forward(self, x):
        dtype = x.dtype
        residual = x
        x = x.float()

        with amp.autocast(device_type="cuda", enabled=False):
            x = self.forward_transform(x).contiguous()
            if self.scale_residual:
                residual = self.inverse_transform(x)
                residual = residual.to(dtype)

        B, C, H, W = x.shape
        x = x.reshape(B, self.num_groups, C // self.num_groups, H, W)
        xp = self._contract(x, self.weight, separable=self.separable, operator_type=self.operator_type)
        x = xp.reshape(B, self.out_channels, H, W).contiguous()

        with amp.autocast(device_type="cuda", enabled=False):
            x = self.inverse_transform(x)

        if hasattr(self, "bias"):
            x = x + self.bias

        x = x.to(dtype=dtype)

        return x, residual


class SpectralAttention(nn.Module):
    """
    Spherical non-linear FNO layer
    """

    def __init__(
        self,
        forward_transform,
        inverse_transform,
        in_channels,
        out_channels,
        operator_type="diagonal",
        hidden_size_factor=2,
        complex_activation="real",
        bias=False,
        spectral_layers=1,
        drop_rate=0.0,
        gain=1.0,
    ):
        super().__init__()

        self.in_channels = in_channels
        self.out_channels = out_channels
        self.operator_type = operator_type
        self.spectral_layers = spectral_layers

        self.modes_lat = forward_transform.lmax
        self.modes_lon = forward_transform.mmax

        # only storing the forward handle to be able to call it
        self.forward_transform = forward_transform
        self.inverse_transform = inverse_transform

        self.scale_residual = (
            (self.forward_transform.nlat != self.inverse_transform.nlat)
            or (self.forward_transform.nlon != self.inverse_transform.nlon)
            or (self.forward_transform.grid != self.inverse_transform.grid)
        )

        assert inverse_transform.lmax == self.modes_lat
        assert inverse_transform.mmax == self.modes_lon

        hidden_size = int(hidden_size_factor * self.in_channels)

        if operator_type == "diagonal":
            self.mul_add_handle = compl_muladd2d_fwd
            self.mul_handle = compl_mul2d_fwd

            # weights
            scale = math.sqrt(2.0 / float(in_channels))
            w = [scale * torch.randn(self.in_channels, hidden_size, dtype=torch.complex64)]
            for l in range(1, self.spectral_layers):
                scale = math.sqrt(2.0 / float(hidden_size))
                w.append(scale * torch.randn(hidden_size, hidden_size, dtype=torch.complex64))
            self.w = nn.ParameterList(w)

            scale = math.sqrt(gain / float(in_channels))
            self.wout = nn.Parameter(scale * torch.randn(hidden_size, self.out_channels, dtype=torch.complex64))

            if bias:
                self.b = nn.ParameterList([scale * torch.randn(hidden_size, 1, 1, dtype=torch.complex64) for _ in range(self.spectral_layers)])

            self.activations = nn.ModuleList([])
            for l in range(0, self.spectral_layers):
                self.activations.append(ComplexReLU(mode=complex_activation, bias_shape=(hidden_size, 1, 1), scale=scale))

        elif operator_type == "l-dependant":
            self.mul_add_handle = compl_exp_muladd2d_fwd
            self.mul_handle = compl_exp_mul2d_fwd

            # weights
            scale = math.sqrt(2.0 / float(in_channels))
            w = [scale * torch.randn(self.modes_lat, self.in_channels, hidden_size, dtype=torch.complex64)]
            for l in range(1, self.spectral_layers):
                scale = math.sqrt(2.0 / float(hidden_size))
                w.append(scale * torch.randn(self.modes_lat, hidden_size, hidden_size, dtype=torch.complex64))
            self.w = nn.ParameterList(w)

            if bias:
                self.b = nn.ParameterList([scale * torch.randn(hidden_size, 1, 1, dtype=torch.complex64) for _ in range(self.spectral_layers)])

            scale = math.sqrt(gain / float(in_channels))
            self.wout = nn.Parameter(scale * torch.randn(self.modes_lat, hidden_size, self.out_channels, dtype=torch.complex64))

            self.activations = nn.ModuleList([])
            for l in range(0, self.spectral_layers):
                self.activations.append(ComplexReLU(mode=complex_activation, bias_shape=(hidden_size, 1, 1), scale=scale))

        else:
            raise ValueError("Unknown operator type")

        self.drop = nn.Dropout(drop_rate) if drop_rate > 0.0 else nn.Identity()

    def forward_mlp(self, x):
        B, C, H, W = x.shape

        xr = torch.view_as_real(x)

        for l in range(self.spectral_layers):
            if hasattr(self, "b"):
                xr = self.mul_add_handle(xr, self.w[l], self.b[l])
            else:
                xr = self.mul_handle(xr, self.w[l])
            xr = torch.view_as_complex(xr)
            xr = self.activations[l](xr)
            xr = self.drop(xr)
            xr = torch.view_as_real(xr)

        # final MLP
        x = self.mul_handle(xr, self.wout)

        x = torch.view_as_complex(x)

        return x

    def forward(self, x):
        dtype = x.dtype
        residual = x
        x = x.to(torch.float32)

        # FWD transform
        with amp.autocast(device_type="cuda", enabled=False):
            x = self.forward_transform(x)
            if self.scale_residual:
                residual = self.inverse_transform(x)
                residual = residual.to(dtype)

        # MLP
        x = self.forward_mlp(x)

        # BWD transform
        with amp.autocast(device_type="cuda", enabled=False):
            x = self.inverse_transform(x)

        # cast back to initial precision
        x = x.to(dtype)

        return x, residual
