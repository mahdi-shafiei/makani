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

from makani.models.preprocessor import Preprocessor2D


class SingleStepWrapper(nn.Module):
    def __init__(self, params, model_handle):
        super().__init__()
        self.preprocessor = Preprocessor2D(params)
        self.model = model_handle()

    def forward(self, inp, replace_state=True):
        # update internal state
        self.preprocessor.update_internal_state(replace_state=replace_state)

        # append unpredicted features
        inpa = self.preprocessor.append_unpredicted_features(inp)

        # now normalize
        self.preprocessor.history_compute_stats(inpa)
        inpan = self.preprocessor.history_normalize(inpa, target=False)

        # now add static features if requested
        inpans = self.preprocessor.add_static_features(inpan)

        # forward pass
        yn = self.model(inpans)

        # perform bias correction if requested
        yn = self.preprocessor.correct_bias(yn)

        # undo normalization
        y = self.preprocessor.history_denormalize(yn, target=True)

        # add residual (for residual learning, no-op for direct learning
        y = self.preprocessor.add_residual(inp, y)

        return y


class MultiStepWrapper(nn.Module):
    def __init__(self, params, model_handle):
        super().__init__()
        self.preprocessor = Preprocessor2D(params)
        self.model = model_handle()
        self.residual_mode = True if (params.target == "target") else False
        self.push_forward_mode = params.get("multistep_push_forward", False)

        # collect parameters for history
        self.n_future = params.n_future

    def _forward_train(self, inp):
        result = []
        inpt = inp

        # initialize fresh buffer
        self.preprocessor.update_internal_state(replace_state=True)

        # do the rollout
        for step in range(self.n_future + 1):

            # in push-forward mode, we need to detach the tensor:
            if self.push_forward_mode:
                inpt = inpt.detach()

            # add unpredicted features
            inpa = self.preprocessor.append_unpredicted_features(inpt)

            # do history normalization
            self.preprocessor.history_compute_stats(inpa)
            inpan = self.preprocessor.history_normalize(inpa, target=False)

            # add static features
            inpans = self.preprocessor.add_static_features(inpan)

            # prediction
            predn = self.model(inpans)

            # perform bias correction if requested
            predn = self.preprocessor.correct_bias(predn)

            # append the denormalized result to output list
            # important to do that here, otherwise normalization stats
            # will have been updated later:
            pred = self.preprocessor.history_denormalize(predn, target=True)

            # add residual (for residual learning, no-op for direct learning
            pred = self.preprocessor.add_residual(inpt, pred)

            # append output
            result.append(pred)

            if step == self.n_future:
                break

            # update internal buffer
            self.preprocessor.update_internal_state(replace_state=False)

            # append history
            inpt = self.preprocessor.append_history(inpt, pred, step)

        # concat the tensors along channel dim to be compatible with flattened target
        result = torch.cat(result, dim=1)

        return result

    def _forward_eval(self, inp):
        # update internal state
        self.preprocessor.update_internal_state(replace_state=True)

        # first append unpredicted features
        inpa = self.preprocessor.append_unpredicted_features(inp)

        # do history normalization
        self.preprocessor.history_compute_stats(inpa)
        inpan = self.preprocessor.history_normalize(inpa, target=False)

        # add static features
        inpans = self.preprocessor.add_static_features(inpan)

        # important, remove normalization here,
        # because otherwise normalization stats are already outdated
        yn = self.model(inpans)

        # perform bias correction if requested
        yn = self.preprocessor.correct_bias(yn)

        # important, remove normalization here,
        # because otherwise normalization stats are already outdated
        y = self.preprocessor.history_denormalize(yn, target=True)

        # add residual (for residual learning, no-op for direct learning
        y = self.preprocessor.add_residual(inp, y)

        return y

    def forward(self, inp):
        # decide which routine to call
        if self.training:
            y = self._forward_train(inp)
        else:
            y = self._forward_eval(inp)

        return y
