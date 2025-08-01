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

import os
import io
import multiprocessing as mp
import numpy as np
import concurrent.futures as cf
from PIL import Image
from moviepy.video.io.ImageSequenceClip import ImageSequenceClip
import wandb

import torch


def plot_comparison(
    pred,
    truth,
    lat=None,
    lon=None,
    pred_title="Prediction",
    truth_title="Ground truth",
    cmap="twilight_shifted",
    projection="mollweide",
    diverging=False,
    figsize=(6, 7),
    vmax=None,
):
    """
    Visualization tool to plot a comparison between ground truth and prediction
    pred: 2d array
    truth: 2d array
    cmap: colormap
    projection: "mollweide", "hammer", "aitoff" or None
    """
    import matplotlib.pyplot as plt

    assert len(pred.shape) == 2
    assert len(truth.shape) == 2
    assert pred.shape == truth.shape

    H, W = pred.shape
    if (lat is None) or (lon is None):
        lon = np.linspace(-np.pi, np.pi, W)
        lat = np.linspace(np.pi / 2.0, -np.pi / 2.0, H)
    Lon, Lat = np.meshgrid(lon, lat)

    # only normalize with the truth
    vmax = vmax or np.abs(truth).max()
    # vmax = vmax or max(np.abs(pred).max(), np.abs(truth).max())
    if diverging:
        vmin = -vmax
    else:
        vmin = 0.0

    fig = plt.figure(figsize=figsize)

    ax = fig.add_subplot(2, 1, 1, projection=projection)  # can also be Mollweide

    ax.pcolormesh(Lon, Lat, pred, cmap=cmap)
    ax.set_title(pred_title)
    ax.grid(True)
    ax.set_xticklabels([])
    ax.set_yticklabels([])

    ax = fig.add_subplot(2, 1, 2, projection=projection)  # can also be Mollweide

    ax.pcolormesh(Lon, Lat, truth, cmap=cmap)
    ax.set_title(truth_title)
    ax.grid(True)
    ax.set_xticklabels([])
    ax.set_yticklabels([])

    plt.tight_layout()

    # save into memory buffer
    buf = io.BytesIO()
    plt.savefig(buf)
    plt.close(fig)
    buf.seek(0)

    # create image
    image = Image.open(buf)

    return image


def plot_rollout_metrics(metric_curves, var_names, score_path=None, file_prefix="curve", dtxdh=6):
    "Plots rollout metrics such as RMSE and ACC and saves them to the experiment directory"

    for ivar, var_name in enumerate(var_names):

        import matplotlib.pyplot as plt
        import matplotlib.ticker as ticker

        metric_curve = metric_curves[ivar]

        # prepare the plot
        fig, ax = plt.subplots()

        # get the time sclaling for the time axis
        t = np.arange(1, len(metric_curve) + 1, 1) * dtxdh
        ax.plot(t, metric_curve, ".-")
        xticks = np.arange(0, len(metric_curve) + 1, 1) * dtxdh
        x_locator = ticker.FixedLocator(xticks)
        ax.xaxis.set_major_locator(x_locator)
        y_locator = ticker.MaxNLocator(nbins=20)
        ax.yaxis.set_major_locator(y_locator)
        ax.grid(which="major", alpha=0.5)
        ax.legend()
        ax.set_xlabel("Time [h]")
        ax.set_ylabel(file_prefix + " " + var_name)
        plt.setp(ax.get_xticklabels(), rotation=45, horizontalalignment="right")

        # write out the plot
        if score_path is not None:
            fig.savefig(os.path.join(score_path, file_prefix + "_" + var_name + ".png"))
        # # push to wandb
        # if params.log_to_wandb:
        #     wandb.log({metric + "_" + var_name: wandb.Image(fig)}, step=epoch)


def visualize_field(tag, func_string, prediction, target, lat, lon, scale, bias, diverging):
    torch.cuda.nvtx.range_push("visualize_field")

    # get func handle:
    func_handle = eval(func_string)

    # unscale:
    pred = scale * prediction + bias
    targ = scale * target + bias

    # apply functor:
    pred = func_handle(pred)
    targ = func_handle(targ)

    # generate image
    image = plot_comparison(pred, targ, lat, lon, pred_title="Prediction", truth_title="Ground truth", projection="mollweide", diverging=diverging)

    torch.cuda.nvtx.range_pop()

    return tag, image


class VisualizationWrapper(object):
    "Handles visualization during training"

    def __init__(self, log_to_wandb, path, prefix, plot_list, lat=None, lon=None, scale=1.0, bias=0.0, num_workers=1):
        self.log_to_wandb = log_to_wandb
        self.generate_video = True
        self.path = path
        self.prefix = prefix
        self.plot_list = plot_list

        # grid
        self.lat = lat
        self.lon = lon

        # normalization
        self.scale = scale
        self.bias = bias

        # this is for parallel processing
        ctx = mp.get_context("spawn")
        self.executor = cf.ProcessPoolExecutor(max_workers=num_workers, mp_context=ctx)
        self.requests = []

    def reset(self):
        self.requests = []

    def add(self, tag, prediction, target):
        # go through the plot list
        for item in self.plot_list:
            field_name = item["name"]
            func_string = item["functor"]
            plot_diverge = item["diverging"]
            self.requests.append(
                self.executor.submit(visualize_field, (tag, field_name), func_string, np.copy(prediction), np.copy(target), self.lat, self.lon, self.scale, self.bias, plot_diverge)
            )

        return

    def finalize(self):
        torch.cuda.nvtx.range_push("VisualizationWrapper:finalize")

        results = {}
        for request in cf.as_completed(self.requests):
            token, image = request.result()
            tag, field_name = token
            prefix = field_name + "_" + tag
            results[prefix] = image

        if self.generate_video:
            if self.log_to_wandb:
                video = []

                # draw stuff that goes on every frame here
                for prefix, image in sorted(results.items()):
                    video.append(np.transpose(np.asarray(image), (2, 0, 1)))

                video = np.stack(video)
                results = [wandb.Video(video, fps=3, format="gif")]
            else:
                video = []

                # draw stuff that goes on every frame here
                for prefix, image in sorted(results.items()):
                    video.append(np.asarray(image))

                video = ImageSequenceClip(video, fps=3)
                video.write_gif("video_output.gif")

        else:
            results = [wandb.Image(image, caption=prefix) for prefix, image in results.items()]

        if self.log_to_wandb and results:
            wandb.log({"Inference samples": results}, commit=False)

        # reset requests
        self.reset()

        torch.cuda.nvtx.range_pop()

        return
