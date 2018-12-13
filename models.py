
import json
import warnings
from collections import OrderedDict
from contextlib import contextmanager
from math import sqrt, exp, log

import torch
import torch.nn as nn
from torch.utils.checkpoint import checkpoint

warnings.simplefilter('ignore')


class BaseModule(nn.Module):
    def __init__(self):
        self.act_fn = None
        super(BaseModule, self).__init__()

    def load_state_dict(self, state_dict, strict=True):
        own_state = self.state_dict()
        for name, param in state_dict.items():
            if name in own_state:
                try:
                    own_state[name].copy_(param.data)
                except Exception as e:
                    print("Parameter {} fails to load.".format(name))
                    print("-----------------------------------------")
                    print(e)
            else:
                print("Parameter {} is not in the model. ".format(name))

    @contextmanager
    def set_activation_inplace(self):
        if hasattr(self, 'act_fn') and hasattr(self.act_fn, 'inplace'):
            # save memory
            self.act_fn.inplace = True
            yield
            self.act_fn.inplace = False
        else:
            yield

    def total_parameters(self):
        return sum([i.numel() for i in self.parameters()])

    def forward(self, *x):
        raise NotImplementedError


class DCSCN(BaseModule):
    # https://github.com/jiny2001/dcscn-super-resolution
    def __init__(self,
                 color_channel=3,
                 up_scale=2,
                 feature_layers=12,
                 first_feature_filters=196,
                 last_feature_filters=48,
                 reconstruction_filters=128,
                 up_sampler_filters=32
                 ):
        super(DCSCN, self).__init__()
        self.total_feature_channels = 0
        self.total_reconstruct_filters = 0
        self.upscale = up_scale

        self.act_fn = nn.SELU(inplace=False)
        self.feature_block = self.make_feature_extraction_block(color_channel,
                                                                feature_layers,
                                                                first_feature_filters,
                                                                last_feature_filters)

        self.reconstruction_block = self.make_reconstruction_block(reconstruction_filters)
        self.up_sampler = self.make_upsampler(up_sampler_filters, color_channel)
        self.selu_init_params()

    def selu_init_params(self):
        for i in self.modules():
            if isinstance(i, nn.Conv2d):
                i.weight.data.normal_(0.0, 1.0 / sqrt(i.weight.numel()))
                if i.bias is not None:
                    i.bias.data.fill_(0)

    def conv_block(self, in_channel, out_channel, kernel_size):
        m = OrderedDict([
            # ("Padding", nn.ReplicationPad2d((kernel_size - 1) // 2)),
            ('Conv2d', nn.Conv2d(in_channel, out_channel, kernel_size=kernel_size, padding=(kernel_size - 1) // 2)),
            ('Activation', self.act_fn)
        ])

        return nn.Sequential(m)

    def make_feature_extraction_block(self, color_channel, num_layers, first_filters, last_filters):
        # input layer
        feature_block = [("Feature 1", self.conv_block(color_channel, first_filters, 3))]
        # exponential decay
        # rest layers
        alpha_rate = log(first_filters / last_filters) / (num_layers - 1)
        filter_nums = [round(first_filters * exp(-alpha_rate * i)) for i in range(num_layers)]

        self.total_feature_channels = sum(filter_nums)

        layer_filters = [[filter_nums[i], filter_nums[i + 1], 3] for i in range(num_layers - 1)]

        feature_block.extend([("Feature {}".format(index + 2), self.conv_block(*x))
                              for index, x in enumerate(layer_filters)])
        return nn.Sequential(OrderedDict(feature_block))

    def make_reconstruction_block(self, num_filters):
        B1 = self.conv_block(self.total_feature_channels, num_filters // 2, 1)
        B2 = self.conv_block(num_filters // 2, num_filters, 3)
        m = OrderedDict([
            ("A", self.conv_block(self.total_feature_channels, num_filters, 1)),
            ("B", nn.Sequential(*[B1, B2]))
        ])
        self.total_reconstruct_filters = num_filters * 2
        return nn.Sequential(m)

    def make_upsampler(self, out_channel, color_channel):
        out = out_channel * self.upscale ** 2
        m = OrderedDict([
            ('Conv2d_block', self.conv_block(self.total_reconstruct_filters, out, kernel_size=3)),
            ('PixelShuffle', nn.PixelShuffle(self.upscale)),
            ("Conv2d", nn.Conv2d(out_channel, color_channel, kernel_size=3, padding=1, bias=False))
        ])

        return nn.Sequential(m)

    def forward(self, x):
        # residual learning
        lr, lr_up = x
        feature = []
        for layer in self.feature_block.children():
            lr = layer(lr)
            feature.append(lr)
        feature = torch.cat(feature, dim=1)

        reconstruction = [layer(feature) for layer in self.reconstruction_block.children()]
        reconstruction = torch.cat(reconstruction, dim=1)

        lr = self.up_sampler(reconstruction)
        return lr + lr_up
