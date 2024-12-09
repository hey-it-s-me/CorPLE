import torch
import torch.nn as nn
import torch.utils.checkpoint as checkpoint
from torch import Tensor
from torch.nn import functional as F

from timm.models.layers import DropPath, trunc_normal_
from einops.layers.torch import Rearrange
from einops import rearrange

import math
import numpy as np

from basicsr.utils.registry import ARCH_REGISTRY

from PIL import Image
import torch
import torch.optim
from torchvision import datasets, transforms
from basicsr.archs.contourlet_transform.pycontourlet import batch_multi_channel_pdfbdec
from torchvision.transforms.functional import to_grayscale

from timm.models.layers import trunc_normal_, DropPath, LayerNorm2d


def stack_same_dim(x):
    """Stack a list/dict of 4D tensors of same img dimension together."""
    # Collect tensor with same dimension into a dict of list
    output = {}

    # Input is list
    if isinstance(x, list):
        for i in range(len(x)):
            if isinstance(x[i], list):
                for j in range(len(x[i])):
                    shape = tuple(x[i][j].shape)
                    if shape in output.keys():
                        output[shape].append(x[i][j])
                    else:
                        output[shape] = [x[i][j]]
            else:
                shape = tuple(x[i].shape)
                if shape in output.keys():
                    output[shape].append(x[i])
                else:
                    output[shape] = [x[i]]
    else:
        for k in x.keys():
            shape = tuple(x[k].shape[2:4])
            if shape in output.keys():
                output[shape].append(x[k])
            else:
                output[shape] = [x[k]]

    # Concat the list of tensors into single tensor
    for k in output.keys():
        output[k] = torch.cat(output[k], dim=1)

    return output


def img2windows(img, H_sp, W_sp):
    """
    Input: Image (B, C, H, W)
    Output: Window Partition (B', N, C)
    """
    B, C, H, W = img.shape
    img_reshape = img.view(B, C, H // H_sp, H_sp, W // W_sp, W_sp)
    img_perm = img_reshape.permute(0, 2, 4, 3, 5, 1).contiguous().reshape(-1, H_sp * W_sp, C)
    return img_perm


def windows2img(img_splits_hw, H_sp, W_sp, H, W):
    """
    Input: Window Partition (B', N, C)
    Output: Image (B, H, W, C)
    """
    B = int(img_splits_hw.shape[0] / (H * W / H_sp / W_sp))

    img = img_splits_hw.view(B, H // H_sp, W // W_sp, H_sp, W_sp, -1)
    img = img.permute(0, 1, 3, 2, 4, 5).contiguous().view(B, H, W, -1)
    return img


class SpatialGate(nn.Module):
    """ Spatial-Gate.
    Args:
        dim (int): Half of input channels.
    """

    def __init__(self, dim):
        super().__init__()
        self.norm = nn.LayerNorm(dim)
        self.conv = nn.Conv2d(dim, dim, kernel_size=3, stride=1, padding=1, groups=dim)  # DW Conv

    def forward(self, x, H, W):
        # Split
        x1, x2 = x.chunk(2, dim=-1)
        B, N, C = x.shape
        x2 = self.conv(self.norm(x2).transpose(1, 2).contiguous().view(B, C // 2, H, W)).flatten(2).transpose(-1,
                                                                                                              -2).contiguous()

        return x1 * x2


class SGFN(nn.Module):
    """ Spatial-Gate Feed-Forward Network.
    Args:
        in_features (int): Number of input channels.
        hidden_features (int | None): Number of hidden channels. Default: None
        out_features (int | None): Number of output channels. Default: None
        act_layer (nn.Module): Activation layer. Default: nn.GELU
        drop (float): Dropout rate. Default: 0.0
    """

    def __init__(self, in_features, hidden_features=None, out_features=None, act_layer=nn.GELU, drop=0.):
        super().__init__()
        out_features = out_features or in_features
        hidden_features = hidden_features or in_features
        self.fc1 = nn.Linear(in_features, hidden_features)
        self.act = act_layer()
        self.sg = SpatialGate(hidden_features // 2)
        self.fc2 = nn.Linear(hidden_features // 2, out_features)
        self.drop = nn.Dropout(drop)

    def forward(self, x, H, W):
        """
        Input: x: (B, H*W, C), H, W
        Output: x: (B, H*W, C)
        """
        x = self.fc1(x)
        x = self.act(x)
        x = self.drop(x)

        x = self.sg(x, H, W)
        x = self.drop(x)

        x = self.fc2(x)
        x = self.drop(x)
        return x


class DynamicPosBias(nn.Module):
    # The implementation builds on Crossformer code https://github.com/cheerss/CrossFormer/blob/main/models/crossformer.py
    """ Dynamic Relative Position Bias.
    Args:
        dim (int): Number of input channels.
        num_heads (int): Number of attention heads.
        residual (bool):  If True, use residual strage to connect conv.
    """

    def __init__(self, dim, num_heads, residual):
        super().__init__()
        self.residual = residual
        self.num_heads = num_heads
        self.pos_dim = dim // 4
        self.pos_proj = nn.Linear(2, self.pos_dim)
        self.pos1 = nn.Sequential(
            nn.LayerNorm(self.pos_dim),
            nn.ReLU(inplace=True),
            nn.Linear(self.pos_dim, self.pos_dim),
        )
        self.pos2 = nn.Sequential(
            nn.LayerNorm(self.pos_dim),
            nn.ReLU(inplace=True),
            nn.Linear(self.pos_dim, self.pos_dim)
        )
        self.pos3 = nn.Sequential(
            nn.LayerNorm(self.pos_dim),
            nn.ReLU(inplace=True),
            nn.Linear(self.pos_dim, self.num_heads)
        )

    def forward(self, biases):
        if self.residual:
            pos = self.pos_proj(biases)  # 2Gh-1 * 2Gw-1, heads
            pos = pos + self.pos1(pos)
            pos = pos + self.pos2(pos)
            pos = self.pos3(pos)
        else:
            pos = self.pos3(self.pos2(self.pos1(self.pos_proj(biases))))
        return pos


class Spatial_Attention(nn.Module):
    """ Spatial Window Self-Attention.
    It supports rectangle window (containing square window).
    Args:
        dim (int): Number of input channels.
        idx (int): The indentix of window. (0/1)
        split_size (tuple(int)): Height and Width of spatial window.
        dim_out (int | None): The dimension of the attention output. Default: None
        num_heads (int): Number of attention heads. Default: 6
        attn_drop (float): Dropout ratio of attention weight. Default: 0.0
        proj_drop (float): Dropout ratio of output. Default: 0.0
        qk_scale (float | None): Override default qk scale of head_dim ** -0.5 if set
        position_bias (bool): The dynamic relative position bias. Default: True
    """

    def __init__(self, dim, idx, split_size=[8, 8], dim_out=None, num_heads=6, attn_drop=0., proj_drop=0.,
                 qk_scale=None, position_bias=True):
        super().__init__()
        self.dim = dim
        self.dim_out = dim_out or dim
        self.split_size = split_size
        self.num_heads = num_heads
        self.idx = idx
        self.position_bias = position_bias

        head_dim = dim // num_heads
        self.scale = qk_scale or head_dim ** -0.5

        if idx == 0:
            H_sp, W_sp = self.split_size[0], self.split_size[1]
        elif idx == 1:
            W_sp, H_sp = self.split_size[0], self.split_size[1]
        else:
            print("ERROR MODE", idx)
            exit(0)
        self.H_sp = H_sp
        self.W_sp = W_sp

        if self.position_bias:
            self.pos = DynamicPosBias(self.dim // 4, self.num_heads, residual=False)
            # generate mother-set
            position_bias_h = torch.arange(1 - self.H_sp, self.H_sp)
            position_bias_w = torch.arange(1 - self.W_sp, self.W_sp)
            biases = torch.stack(torch.meshgrid([position_bias_h, position_bias_w]))
            biases = biases.flatten(1).transpose(0, 1).contiguous().float()
            self.register_buffer('rpe_biases', biases)

            # get pair-wise relative position index for each token inside the window
            coords_h = torch.arange(self.H_sp)
            coords_w = torch.arange(self.W_sp)
            coords = torch.stack(torch.meshgrid([coords_h, coords_w]))
            coords_flatten = torch.flatten(coords, 1)
            relative_coords = coords_flatten[:, :, None] - coords_flatten[:, None, :]
            relative_coords = relative_coords.permute(1, 2, 0).contiguous()
            relative_coords[:, :, 0] += self.H_sp - 1
            relative_coords[:, :, 1] += self.W_sp - 1
            relative_coords[:, :, 0] *= 2 * self.W_sp - 1
            relative_position_index = relative_coords.sum(-1)
            self.register_buffer('relative_position_index', relative_position_index)

        self.attn_drop = nn.Dropout(attn_drop)

    def im2win(self, x, H, W):
        B, N, C = x.shape
        x = x.transpose(-2, -1).contiguous().view(B, C, H, W)
        x = img2windows(x, self.H_sp, self.W_sp)
        x = x.reshape(-1, self.H_sp * self.W_sp, self.num_heads, C // self.num_heads).permute(0, 2, 1, 3).contiguous()
        return x

    def forward(self, qkv, H, W, mask=None):
        """
        Input: qkv: (B, 3*L, C), H, W, mask: (B, N, N), N is the window size
        Output: x (B, H, W, C)
        """
        q, k, v = qkv[0], qkv[1], qkv[2]

        B, L, C = q.shape
        assert L == H * W, "flatten img_tokens has wrong size"

        # partition the q,k,v, image to window
        q = self.im2win(q, H, W)
        k = self.im2win(k, H, W)
        v = self.im2win(v, H, W)

        q = q * self.scale
        attn = (q @ k.transpose(-2, -1))  # B head N C @ B head C N --> B head N N

        # calculate drpe
        if self.position_bias:
            pos = self.pos(self.rpe_biases)
            # select position bias
            relative_position_bias = pos[self.relative_position_index.view(-1)].view(
                self.H_sp * self.W_sp, self.H_sp * self.W_sp, -1)
            relative_position_bias = relative_position_bias.permute(2, 0, 1).contiguous()
            attn = attn + relative_position_bias.unsqueeze(0)

        N = attn.shape[3]

        # use mask for shift window
        if mask is not None:
            nW = mask.shape[0]
            attn = attn.view(B, nW, self.num_heads, N, N) + mask.unsqueeze(1).unsqueeze(0)
            attn = attn.view(-1, self.num_heads, N, N)

        attn = nn.functional.softmax(attn, dim=-1, dtype=attn.dtype)
        attn = self.attn_drop(attn)

        x = (attn @ v)
        x = x.transpose(1, 2).reshape(-1, self.H_sp * self.W_sp, C)  # B head N N @ B head N C

        # merge the window, window to image
        x = windows2img(x, self.H_sp, self.W_sp, H, W)  # B H' W' C

        return x


class Adaptive_Spatial_Attention(nn.Module):
    # The implementation builds on CAT code https://github.com/Zhengchen1999/CAT
    """ Adaptive Spatial Self-Attention
    Args:
        dim (int): Number of input channels.
        num_heads (int): Number of attention heads. Default: 6
        split_size (tuple(int)): Height and Width of spatial window.
        shift_size (tuple(int)): Shift size for spatial window.
        qkv_bias (bool): If True, add a learnable bias to query, key, value. Default: True
        qk_scale (float | None): Override default qk scale of head_dim ** -0.5 if set.
        drop (float): Dropout rate. Default: 0.0
        attn_drop (float): Attention dropout rate. Default: 0.0
        rg_idx (int): The indentix of Residual Group (RG)
        b_idx (int): The indentix of Block in each RG
    """

    def __init__(self, dim, num_heads,
                 reso=64, split_size=[8, 8], shift_size=[1, 2], qkv_bias=False, qk_scale=None,
                 drop=0., attn_drop=0., rg_idx=0, b_idx=0):
        super().__init__()
        self.dim = dim
        self.num_heads = num_heads
        self.split_size = split_size
        self.shift_size = shift_size
        self.b_idx = b_idx
        self.rg_idx = rg_idx
        self.patches_resolution = reso
        self.qkv = nn.Linear(dim, dim * 3, bias=qkv_bias)

        assert 0 <= self.shift_size[0] < self.split_size[0], "shift_size must in 0-split_size0"
        assert 0 <= self.shift_size[1] < self.split_size[1], "shift_size must in 0-split_size1"

        self.branch_num = 2

        self.proj = nn.Linear(dim, dim)
        self.proj_drop = nn.Dropout(drop)

        self.attns = nn.ModuleList([
            Spatial_Attention(
                dim // 2, idx=i,
                split_size=split_size, num_heads=num_heads // 2, dim_out=dim // 2,
                qk_scale=qk_scale, attn_drop=attn_drop, proj_drop=drop, position_bias=True)
            for i in range(self.branch_num)])

        if (self.rg_idx % 2 == 0 and self.b_idx > 0 and (self.b_idx - 2) % 4 == 0) or (
                self.rg_idx % 2 != 0 and self.b_idx % 4 == 0):
            attn_mask = self.calculate_mask(self.patches_resolution, self.patches_resolution)
            self.register_buffer("attn_mask_0", attn_mask[0])
            self.register_buffer("attn_mask_1", attn_mask[1])
        else:
            attn_mask = None
            self.register_buffer("attn_mask_0", None)
            self.register_buffer("attn_mask_1", None)

        self.dwconv = nn.Sequential(
            nn.Conv2d(dim, dim, kernel_size=3, stride=1, padding=1, groups=dim),
            nn.BatchNorm2d(dim),
            nn.GELU()
        )
        self.channel_interaction = nn.Sequential(
            nn.AdaptiveAvgPool2d(1),
            nn.Conv2d(dim, dim // 8, kernel_size=1),
            nn.BatchNorm2d(dim // 8),
            nn.GELU(),
            nn.Conv2d(dim // 8, dim, kernel_size=1),
        )
        self.spatial_interaction = nn.Sequential(
            nn.Conv2d(dim, dim // 16, kernel_size=1),
            nn.BatchNorm2d(dim // 16),
            nn.GELU(),
            nn.Conv2d(dim // 16, 1, kernel_size=1)
        )

    def calculate_mask(self, H, W):
        # The implementation builds on Swin Transformer code https://github.com/microsoft/Swin-Transformer/blob/main/models/swin_transformer.py
        # calculate attention mask for shift window
        img_mask_0 = torch.zeros((1, H, W, 1))  # 1 H W 1 idx=0
        img_mask_1 = torch.zeros((1, H, W, 1))  # 1 H W 1 idx=1
        h_slices_0 = (slice(0, -self.split_size[0]),
                      slice(-self.split_size[0], -self.shift_size[0]),
                      slice(-self.shift_size[0], None))
        w_slices_0 = (slice(0, -self.split_size[1]),
                      slice(-self.split_size[1], -self.shift_size[1]),
                      slice(-self.shift_size[1], None))

        h_slices_1 = (slice(0, -self.split_size[1]),
                      slice(-self.split_size[1], -self.shift_size[1]),
                      slice(-self.shift_size[1], None))
        w_slices_1 = (slice(0, -self.split_size[0]),
                      slice(-self.split_size[0], -self.shift_size[0]),
                      slice(-self.shift_size[0], None))
        cnt = 0
        for h in h_slices_0:
            for w in w_slices_0:
                img_mask_0[:, h, w, :] = cnt
                cnt += 1
        cnt = 0
        for h in h_slices_1:
            for w in w_slices_1:
                img_mask_1[:, h, w, :] = cnt
                cnt += 1

        # calculate mask for window-0
        img_mask_0 = img_mask_0.view(1, H // self.split_size[0], self.split_size[0], W // self.split_size[1],
                                     self.split_size[1], 1)
        img_mask_0 = img_mask_0.permute(0, 1, 3, 2, 4, 5).contiguous().view(-1, self.split_size[0], self.split_size[1],
                                                                            1)  # nW, sw[0], sw[1], 1
        mask_windows_0 = img_mask_0.view(-1, self.split_size[0] * self.split_size[1])
        attn_mask_0 = mask_windows_0.unsqueeze(1) - mask_windows_0.unsqueeze(2)
        attn_mask_0 = attn_mask_0.masked_fill(attn_mask_0 != 0, float(-100.0)).masked_fill(attn_mask_0 == 0, float(0.0))

        # calculate mask for window-1
        img_mask_1 = img_mask_1.view(1, H // self.split_size[1], self.split_size[1], W // self.split_size[0],
                                     self.split_size[0], 1)
        img_mask_1 = img_mask_1.permute(0, 1, 3, 2, 4, 5).contiguous().view(-1, self.split_size[1], self.split_size[0],
                                                                            1)  # nW, sw[1], sw[0], 1
        mask_windows_1 = img_mask_1.view(-1, self.split_size[1] * self.split_size[0])
        attn_mask_1 = mask_windows_1.unsqueeze(1) - mask_windows_1.unsqueeze(2)
        attn_mask_1 = attn_mask_1.masked_fill(attn_mask_1 != 0, float(-100.0)).masked_fill(attn_mask_1 == 0, float(0.0))

        return attn_mask_0, attn_mask_1

    def forward(self, x, H, W):
        """
        Input: x: (B, H*W, C), H, W
        Output: x: (B, H*W, C)
        """
        B, L, C = x.shape
        assert L == H * W, "flatten img_tokens has wrong size"

        qkv = self.qkv(x).reshape(B, -1, 3, C).permute(2, 0, 1, 3)  # 3, B, HW, C
        # V without partition
        v = qkv[2].transpose(-2, -1).contiguous().view(B, C, H, W)

        # image padding
        max_split_size = max(self.split_size[0], self.split_size[1])
        pad_l = pad_t = 0
        pad_r = (max_split_size - W % max_split_size) % max_split_size
        pad_b = (max_split_size - H % max_split_size) % max_split_size

        qkv = qkv.reshape(3 * B, H, W, C).permute(0, 3, 1, 2)  # 3B C H W
        qkv = F.pad(qkv, (pad_l, pad_r, pad_t, pad_b)).reshape(3, B, C, -1).transpose(-2, -1)  # l r t b
        _H = pad_b + H
        _W = pad_r + W
        _L = _H * _W

        # window-0 and window-1 on split channels [C/2, C/2]; for square windows (e.g., 8x8), window-0 and window-1 can be merged
        # shift in block: (0, 4, 8, ...), (2, 6, 10, ...), (0, 4, 8, ...), (2, 6, 10, ...), ...
        if (self.rg_idx % 2 == 0 and self.b_idx > 0 and (self.b_idx - 2) % 4 == 0) or (
                self.rg_idx % 2 != 0 and self.b_idx % 4 == 0):
            qkv = qkv.view(3, B, _H, _W, C)
            qkv_0 = torch.roll(qkv[:, :, :, :, :C // 2], shifts=(-self.shift_size[0], -self.shift_size[1]), dims=(2, 3))
            qkv_0 = qkv_0.view(3, B, _L, C // 2)
            qkv_1 = torch.roll(qkv[:, :, :, :, C // 2:], shifts=(-self.shift_size[1], -self.shift_size[0]), dims=(2, 3))
            qkv_1 = qkv_1.view(3, B, _L, C // 2)

            if self.patches_resolution != _H or self.patches_resolution != _W:
                mask_tmp = self.calculate_mask(_H, _W)
                x1_shift = self.attns[0](qkv_0, _H, _W, mask=mask_tmp[0].to(x.device))
                x2_shift = self.attns[1](qkv_1, _H, _W, mask=mask_tmp[1].to(x.device))
            else:
                x1_shift = self.attns[0](qkv_0, _H, _W, mask=self.attn_mask_0)
                x2_shift = self.attns[1](qkv_1, _H, _W, mask=self.attn_mask_1)

            x1 = torch.roll(x1_shift, shifts=(self.shift_size[0], self.shift_size[1]), dims=(1, 2))
            x2 = torch.roll(x2_shift, shifts=(self.shift_size[1], self.shift_size[0]), dims=(1, 2))
            x1 = x1[:, :H, :W, :].reshape(B, L, C // 2)
            x2 = x2[:, :H, :W, :].reshape(B, L, C // 2)
            # attention output
            attened_x = torch.cat([x1, x2], dim=2)

        else:
            x1 = self.attns[0](qkv[:, :, :, :C // 2], _H, _W)[:, :H, :W, :].reshape(B, L, C // 2)
            x2 = self.attns[1](qkv[:, :, :, C // 2:], _H, _W)[:, :H, :W, :].reshape(B, L, C // 2)
            # attention output
            attened_x = torch.cat([x1, x2], dim=2)

        # convolution output
        conv_x = self.dwconv(v)

        # Adaptive Interaction Module (AIM)
        # C-Map (before sigmoid)
        channel_map = self.channel_interaction(conv_x).permute(0, 2, 3, 1).contiguous().view(B, 1, C)
        # S-Map (before sigmoid)
        attention_reshape = attened_x.transpose(-2, -1).contiguous().view(B, C, H, W)
        spatial_map = self.spatial_interaction(attention_reshape)

        # C-I
        attened_x = attened_x * torch.sigmoid(channel_map)
        # S-I
        conv_x = torch.sigmoid(spatial_map) * conv_x
        conv_x = conv_x.permute(0, 2, 3, 1).contiguous().view(B, L, C)

        x = attened_x + conv_x

        x = self.proj(x)
        x = self.proj_drop(x)

        return x


class Adaptive_Channel_Attention(nn.Module):
    # The implementation builds on XCiT code https://github.com/facebookresearch/xcit
    """ Adaptive Channel Self-Attention
    Args:
        dim (int): Number of input channels.
        num_heads (int): Number of attention heads. Default: 6
        qkv_bias (bool): If True, add a learnable bias to query, key, value. Default: True
        qk_scale (float | None): Override default qk scale of head_dim ** -0.5 if set.
        attn_drop (float): Attention dropout rate. Default: 0.0
        drop_path (float): Stochastic depth rate. Default: 0.0
    """

    def __init__(self, dim, num_heads=8, qkv_bias=False, qk_scale=None, attn_drop=0., proj_drop=0.):
        super().__init__()
        self.num_heads = num_heads
        self.temperature = nn.Parameter(torch.ones(num_heads, 1, 1))

        self.qkv = nn.Linear(dim, dim * 3, bias=qkv_bias)
        self.attn_drop = nn.Dropout(attn_drop)
        self.proj = nn.Linear(dim, dim)
        self.proj_drop = nn.Dropout(proj_drop)

        self.dwconv = nn.Sequential(
            nn.Conv2d(dim, dim, kernel_size=3, stride=1, padding=1, groups=dim),
            nn.BatchNorm2d(dim),
            nn.GELU()
        )
        self.channel_interaction = nn.Sequential(
            nn.AdaptiveAvgPool2d(1),
            nn.Conv2d(dim, dim // 8, kernel_size=1),
            nn.BatchNorm2d(dim // 8),
            nn.GELU(),
            nn.Conv2d(dim // 8, dim, kernel_size=1),
        )
        self.spatial_interaction = nn.Sequential(
            nn.Conv2d(dim, dim // 16, kernel_size=1),
            nn.BatchNorm2d(dim // 16),
            nn.GELU(),
            nn.Conv2d(dim // 16, 1, kernel_size=1)
        )

    def forward(self, x, H, W):
        """
        Input: x: (B, H*W, C), H, W
        Output: x: (B, H*W, C)
        """
        B, N, C = x.shape
        qkv = self.qkv(x).reshape(B, N, 3, self.num_heads, C // self.num_heads)
        qkv = qkv.permute(2, 0, 3, 1, 4)
        q, k, v = qkv[0], qkv[1], qkv[2]

        q = q.transpose(-2, -1)
        k = k.transpose(-2, -1)
        v = v.transpose(-2, -1)

        v_ = v.reshape(B, C, N).contiguous().view(B, C, H, W)

        q = torch.nn.functional.normalize(q, dim=-1)
        k = torch.nn.functional.normalize(k, dim=-1)

        attn = (q @ k.transpose(-2, -1)) * self.temperature
        attn = attn.softmax(dim=-1)
        attn = self.attn_drop(attn)

        # attention output
        attened_x = (attn @ v).permute(0, 3, 1, 2).reshape(B, N, C)

        # convolution output
        conv_x = self.dwconv(v_)

        # Adaptive Interaction Module (AIM)
        # C-Map (before sigmoid)
        attention_reshape = attened_x.transpose(-2, -1).contiguous().view(B, C, H, W)
        channel_map = self.channel_interaction(attention_reshape)
        # S-Map (before sigmoid)
        spatial_map = self.spatial_interaction(conv_x).permute(0, 2, 3, 1).contiguous().view(B, N, 1)

        # S-I
        attened_x = attened_x * torch.sigmoid(spatial_map)
        # C-I
        conv_x = conv_x * torch.sigmoid(channel_map)
        conv_x = conv_x.permute(0, 2, 3, 1).contiguous().view(B, N, C)

        x = attened_x + conv_x

        x = self.proj(x)
        x = self.proj_drop(x)

        return x


class PosEmbMLPSwinv1D(nn.Module):
    def __init__(self,
                 dim,
                 rank=2,
                 seq_length=4,
                 conv=False):
        super().__init__()
        self.rank = rank
        if not conv:
            self.cpb_mlp = nn.Sequential(nn.Linear(self.rank, 512, bias=True),
                                         nn.ReLU(),
                                         nn.Linear(512, dim, bias=False))
        else:
            self.cpb_mlp = nn.Sequential(nn.Conv1d(self.rank, 512, 1, bias=True),
                                         nn.ReLU(),
                                         nn.Conv1d(512, dim, 1, bias=False))
        self.grid_exists = False
        self.pos_emb = None
        self.deploy = False
        relative_bias = torch.zeros(1, seq_length, dim)
        self.register_buffer("relative_bias", relative_bias)
        self.conv = conv

    def switch_to_deploy(self):
        self.deploy = True

    def forward(self, input_tensor):
        seq_length = input_tensor.shape[1] if not self.conv else input_tensor.shape[2]
        if self.deploy:
            return input_tensor + self.relative_bias
        else:
            self.grid_exists = False
        if not self.grid_exists:
            self.grid_exists = True
            if self.rank == 1:
                relative_coords_h = torch.arange(0, seq_length, device=input_tensor.device, dtype=input_tensor.dtype)
                relative_coords_h -= seq_length // 2
                relative_coords_h /= (seq_length // 2)
                relative_coords_table = relative_coords_h
                self.pos_emb = self.cpb_mlp(relative_coords_table.unsqueeze(0).unsqueeze(2))
                self.relative_bias = self.pos_emb
            else:
                seq_length = int(seq_length ** 0.5)
                relative_coords_h = torch.arange(0, seq_length, device=input_tensor.device, dtype=input_tensor.dtype)
                relative_coords_w = torch.arange(0, seq_length, device=input_tensor.device, dtype=input_tensor.dtype)
                relative_coords_table = torch.stack(
                    torch.meshgrid([relative_coords_h, relative_coords_w])).contiguous().unsqueeze(0)
                relative_coords_table -= seq_length // 2
                relative_coords_table /= (seq_length // 2)
                if not self.conv:
                    self.pos_emb = self.cpb_mlp(relative_coords_table.flatten(2).transpose(1, 2))
                else:
                    self.pos_emb = self.cpb_mlp(relative_coords_table.flatten(2))
                self.relative_bias = self.pos_emb
        input_tensor = input_tensor + self.pos_emb
        return input_tensor


class Mlp(nn.Module):
    """
    Multi-Layer Perceptron (MLP) block
    """

    def __init__(self,
                 in_features,
                 hidden_features=None,
                 out_features=None,
                 act_layer=nn.GELU,
                 drop=0.):
        """
        Args:
            in_features: input features dimension.
            hidden_features: hidden features dimension.
            out_features: output features dimension.
            act_layer: activation function.
            drop: dropout rate.
        """

        super().__init__()
        out_features = out_features or in_features
        hidden_features = hidden_features or in_features
        self.fc1 = nn.Linear(in_features, hidden_features)
        self.act = act_layer()
        self.fc2 = nn.Linear(hidden_features, out_features)
        self.drop = nn.Dropout(drop)

    def forward(self, x):
        x_size = x.size()
        x = x.view(-1, x_size[-1])
        x = self.fc1(x)
        x = self.act(x)
        x = self.drop(x)
        x = self.fc2(x)
        x = self.drop(x)
        x = x.view(x_size)
        return x


class PosEmbMLPSwinv2D(nn.Module):
    def __init__(self,
                 window_size,
                 pretrained_window_size,
                 num_heads, seq_length,
                 ct_correct=False,
                 no_log=False):
        super().__init__()
        self.window_size = window_size
        self.num_heads = num_heads
        self.cpb_mlp = nn.Sequential(nn.Linear(2, 512, bias=True),
                                     nn.ReLU(inplace=True),
                                     nn.Linear(512, num_heads, bias=False))
        relative_coords_h = torch.arange(-(self.window_size[0] - 1), self.window_size[0], dtype=torch.float32)
        relative_coords_w = torch.arange(-(self.window_size[1] - 1), self.window_size[1], dtype=torch.float32)
        relative_coords_table = torch.stack(
            torch.meshgrid([relative_coords_h,
                            relative_coords_w])).permute(1, 2, 0).contiguous().unsqueeze(0)  # 1, 2*Wh-1, 2*Ww-1, 2
        if pretrained_window_size[0] > 0:
            relative_coords_table[:, :, :, 0] /= (pretrained_window_size[0] - 1)
            relative_coords_table[:, :, :, 1] /= (pretrained_window_size[1] - 1)
        else:
            relative_coords_table[:, :, :, 0] /= (self.window_size[0] - 1)
            relative_coords_table[:, :, :, 1] /= (self.window_size[1] - 1)

        if not no_log:
            relative_coords_table *= 8  # normalize to -8, 8
            relative_coords_table = torch.sign(relative_coords_table) * torch.log2(
                torch.abs(relative_coords_table) + 1.0) / np.log2(8)

        self.register_buffer("relative_coords_table", relative_coords_table)
        coords_h = torch.arange(self.window_size[0])
        coords_w = torch.arange(self.window_size[1])
        coords = torch.stack(torch.meshgrid([coords_h, coords_w]))
        coords_flatten = torch.flatten(coords, 1)
        relative_coords = coords_flatten[:, :, None] - coords_flatten[:, None, :]
        relative_coords = relative_coords.permute(1, 2, 0).contiguous()
        relative_coords[:, :, 0] += self.window_size[0] - 1
        relative_coords[:, :, 1] += self.window_size[1] - 1
        relative_coords[:, :, 0] *= 2 * self.window_size[1] - 1
        relative_position_index = relative_coords.sum(-1)
        self.register_buffer("relative_position_index", relative_position_index)
        self.grid_exists = False
        self.pos_emb = None
        self.deploy = False
        relative_bias = torch.zeros(1, num_heads, seq_length, seq_length)
        self.seq_length = seq_length
        self.register_buffer("relative_bias", relative_bias)
        self.ct_correct = ct_correct

    def switch_to_deploy(self):
        self.deploy = True

    def forward(self, input_tensor, local_window_size):
        if self.deploy:
            input_tensor += self.relative_bias
            return input_tensor
        else:
            self.grid_exists = False

        if not self.grid_exists:
            self.grid_exists = True

            relative_position_bias_table = self.cpb_mlp(self.relative_coords_table).view(-1, self.num_heads)
            relative_position_bias = relative_position_bias_table[self.relative_position_index.view(-1)].view(
                self.window_size[0] * self.window_size[1], self.window_size[0] * self.window_size[1],
                -1)
            relative_position_bias = relative_position_bias.permute(2, 0, 1).contiguous()
            relative_position_bias = 16 * torch.sigmoid(relative_position_bias)
            n_global_feature = input_tensor.shape[2] - local_window_size
            if n_global_feature > 0 and self.ct_correct:

                step_for_ct = self.window_size[0] / (n_global_feature ** 0.5 + 1)
                seq_length = int(n_global_feature ** 0.5)
                indices = []
                for i in range(seq_length):
                    for j in range(seq_length):
                        ind = (i + 1) * step_for_ct * self.window_size[0] + (j + 1) * step_for_ct
                        indices.append(int(ind))

                top_part = relative_position_bias[:, indices, :]
                lefttop_part = relative_position_bias[:, indices, :][:, :, indices]
                left_part = relative_position_bias[:, :, indices]
            relative_position_bias = torch.nn.functional.pad(relative_position_bias, (n_global_feature,
                                                                                      0,
                                                                                      n_global_feature,
                                                                                      0)).contiguous()
            if n_global_feature > 0 and self.ct_correct:
                relative_position_bias = relative_position_bias * 0.0
                relative_position_bias[:, :n_global_feature, :n_global_feature] = lefttop_part
                relative_position_bias[:, :n_global_feature, n_global_feature:] = top_part
                relative_position_bias[:, n_global_feature:, :n_global_feature] = left_part

            self.pos_emb = relative_position_bias.unsqueeze(0)
            self.relative_bias = self.pos_emb

        input_tensor += self.pos_emb
        return input_tensor


class WindowAttention(nn.Module):
    def __init__(self,
                 dim,
                 num_heads=8,
                 qkv_bias=False,
                 qk_scale=None,
                 attn_drop=0.,
                 proj_drop=0.,
                 resolution=0,
                 seq_length=0):
        super().__init__()

        self.num_heads = num_heads
        head_dim = dim // num_heads
        self.head_dim = dim // num_heads
        self.scale = qk_scale or head_dim ** -0.5
        self.qkv = nn.Linear(dim, dim * 3, bias=qkv_bias)
        self.attn_drop = nn.Dropout(attn_drop)
        self.proj = nn.Linear(dim, dim)
        self.proj_drop = nn.Dropout(proj_drop)
        # attention positional bias
        self.pos_emb_funct = PosEmbMLPSwinv2D(window_size=[resolution, resolution],
                                              pretrained_window_size=[resolution, resolution],
                                              num_heads=num_heads,
                                              seq_length=seq_length)

        self.resolution = resolution

    def forward(self, x):
        B, N, C = x.shape
        qkv = self.qkv(x).reshape(B, -1, 3, self.num_heads, C // self.num_heads).permute(2, 0, 3, 1, 4)
        q, k, v = qkv[0], qkv[1], qkv[2]
        attn = (q @ k.transpose(-2, -1)) * self.scale
        attn = self.pos_emb_funct(attn, self.resolution ** 2)
        attn = attn.softmax(dim=-1)
        attn = self.attn_drop(attn)
        x = (attn @ v).transpose(1, 2).reshape(B, -1, C)
        x = self.proj(x)
        x = self.proj_drop(x)
        return x


class PosEmbMLPSwinv1D(nn.Module):
    def __init__(self,
                 dim,
                 rank=2,
                 seq_length=4,
                 conv=False):
        super().__init__()
        self.rank = rank
        if not conv:
            self.cpb_mlp = nn.Sequential(nn.Linear(self.rank, 512, bias=True),
                                         nn.ReLU(),
                                         nn.Linear(512, dim, bias=False))
        else:
            self.cpb_mlp = nn.Sequential(nn.Conv1d(self.rank, 512, 1, bias=True),
                                         nn.ReLU(),
                                         nn.Conv1d(512, dim, 1, bias=False))
        self.grid_exists = False
        self.pos_emb = None
        self.deploy = False
        relative_bias = torch.zeros(1, seq_length, dim)
        self.register_buffer("relative_bias", relative_bias)
        self.conv = conv

    def switch_to_deploy(self):
        self.deploy = True

    def forward(self, input_tensor):
        seq_length = input_tensor.shape[1] if not self.conv else input_tensor.shape[2]
        if self.deploy:
            return input_tensor + self.relative_bias
        else:
            self.grid_exists = False
        if not self.grid_exists:
            self.grid_exists = True
            if self.rank == 1:
                relative_coords_h = torch.arange(0, seq_length, device=input_tensor.device, dtype=input_tensor.dtype)
                relative_coords_h -= seq_length // 2
                relative_coords_h /= (seq_length // 2)
                relative_coords_table = relative_coords_h
                self.pos_emb = self.cpb_mlp(relative_coords_table.unsqueeze(0).unsqueeze(2))
                self.relative_bias = self.pos_emb
            else:
                seq_length = int(seq_length ** 0.5)
                relative_coords_h = torch.arange(0, seq_length, device=input_tensor.device, dtype=input_tensor.dtype)
                relative_coords_w = torch.arange(0, seq_length, device=input_tensor.device, dtype=input_tensor.dtype)
                relative_coords_table = torch.stack(
                    torch.meshgrid([relative_coords_h, relative_coords_w])).contiguous().unsqueeze(0)
                relative_coords_table -= seq_length // 2
                relative_coords_table /= (seq_length // 2)
                if not self.conv:
                    self.pos_emb = self.cpb_mlp(relative_coords_table.flatten(2).transpose(1, 2))
                else:
                    self.pos_emb = self.cpb_mlp(relative_coords_table.flatten(2))
                self.relative_bias = self.pos_emb
        input_tensor = input_tensor + self.pos_emb
        return input_tensor


class TokenInitializer(nn.Module):
    def __init__(self,
                 dim,
                 input_resolution,
                 window_size,
                 ct_size=1):
        super().__init__()

        output_size = int(ct_size * input_resolution / window_size)
        stride_size = int(input_resolution / output_size)
        kernel_size = input_resolution - (output_size - 1) * stride_size
        self.pos_embed = nn.Conv2d(dim, dim, 3, padding=1, groups=dim)
        to_global_feature = nn.Sequential()
        to_global_feature.add_module("pos", self.pos_embed)
        to_global_feature.add_module("pool", nn.AvgPool2d(kernel_size=kernel_size, stride=stride_size))
        self.to_global_feature = to_global_feature
        self.window_size = ct_size

    def forward(self, x):
        x = self.to_global_feature(x)
        B, C, H, W = x.shape
        ct = x.view(B, C, H // self.window_size, self.window_size, W // self.window_size, self.window_size)
        ct = ct.permute(0, 2, 4, 3, 5, 1).reshape(-1, H * W, C)
        return ct


def window_partition(x, window_size):
    B, C, H, W = x.shape
    pad_h = (window_size - H % window_size) % window_size
    pad_w = (window_size - W % window_size) % window_size

    if pad_h > 0 or pad_w > 0:
        x = F.pad(x, (0, pad_w, 0, pad_h))

    H_padded, W_padded = x.shape[2], x.shape[3]
    assert H_padded % window_size == 0 and W_padded % window_size == 0, "Padding failed to make H and W divisible by window_size"

    x = x.view(B, C, H_padded // window_size, window_size, W_padded // window_size, window_size)
    windows = x.permute(0, 2, 4, 3, 5, 1).reshape(-1, window_size * window_size, C)

    return windows


def window_reverse(windows, window_size, H, W, B):
    pad_h = (window_size - H % window_size) % window_size
    pad_w = (window_size - W % window_size) % window_size
    H_padded = H + pad_h
    W_padded = W + pad_w

    x = windows.view(B, H_padded // window_size, W_padded // window_size, window_size, window_size, -1)
    x = x.permute(0, 5, 1, 3, 2, 4).reshape(B, windows.shape[2], H_padded, W_padded)
    if pad_h > 0 or pad_w > 0:
        x = x[:, :, :H, :W]

    return x


class HAT(nn.Module):
    def __init__(self,
                 dim,
                 num_heads,
                 mlp_ratio=4.,
                 qkv_bias=False,
                 qk_scale=None,
                 drop=0.,
                 attn_drop=0.,
                 drop_path=0.,
                 act_layer=nn.GELU,
                 norm_layer=nn.LayerNorm,
                 sr_ratio=1.,
                 window_size=8,
                 last=False,
                 layer_scale=None,
                 ct_size=1,
                 do_propagation=False):
        super().__init__()
        # positional encoding for windowed attention tokens
        self.pos_embed = PosEmbMLPSwinv1D(dim, rank=2, seq_length=window_size ** 2)
        self.norm1 = norm_layer(dim)
        # number of carrier tokens per every window
        cr_tokens_per_window = ct_size ** 2 if sr_ratio > 1 else 0
        # total number of carrier tokens
        cr_tokens_total = cr_tokens_per_window * sr_ratio * sr_ratio
        self.cr_window = ct_size
        self.attn = WindowAttention(dim,
                                    num_heads=num_heads,
                                    qkv_bias=qkv_bias,
                                    qk_scale=qk_scale,
                                    attn_drop=attn_drop,
                                    proj_drop=drop,
                                    resolution=window_size,
                                    seq_length=window_size ** 2 + cr_tokens_per_window)

        self.drop_path = DropPath(drop_path) if drop_path > 0. else nn.Identity()
        self.norm2 = norm_layer(dim)
        mlp_hidden_dim = int(dim * mlp_ratio)
        self.mlp = Mlp(in_features=dim, hidden_features=mlp_hidden_dim, act_layer=act_layer, drop=drop)
        self.window_size = window_size

        use_layer_scale = layer_scale is not None and type(layer_scale) in [int, float]
        self.gamma3 = nn.Parameter(layer_scale * torch.ones(dim)) if use_layer_scale else 1
        self.gamma4 = nn.Parameter(layer_scale * torch.ones(dim)) if use_layer_scale else 1

        self.sr_ratio = sr_ratio
        if sr_ratio > 1:
            # if do hierarchical attention, this part is for carrier tokens
            self.hat_norm1 = norm_layer(dim)
            self.hat_norm2 = norm_layer(dim)
            self.hat_attn = WindowAttention(
                dim,
                num_heads=num_heads, qkv_bias=qkv_bias, qk_scale=qk_scale,
                attn_drop=attn_drop, proj_drop=drop, resolution=int(cr_tokens_total ** 0.5),
                seq_length=cr_tokens_total)

            self.hat_mlp = Mlp(in_features=dim, hidden_features=mlp_hidden_dim, act_layer=act_layer, drop=drop)
            self.hat_drop_path = DropPath(drop_path) if drop_path > 0. else nn.Identity()
            self.hat_pos_embed = PosEmbMLPSwinv1D(dim, rank=2, seq_length=cr_tokens_total)
            self.gamma1 = nn.Parameter(layer_scale * torch.ones(dim)) if use_layer_scale else 1
            self.gamma2 = nn.Parameter(layer_scale * torch.ones(dim)) if use_layer_scale else 1
            self.upsampler = nn.Upsample(size=window_size, mode='nearest')

        # keep track for the last block to explicitly add carrier tokens to feature maps
        self.last = last
        self.do_propagation = do_propagation

    def forward(self, x, carrier_tokens):
        B, T, N = x.shape
        ct = carrier_tokens
        x = self.pos_embed(x)

        if self.sr_ratio > 1:
            # do hierarchical attention via carrier tokens
            # first do attention for carrier tokens
            Bg, Ng, Hg = ct.shape

            # ct are located quite differently
            ct = ct_dewindow(ct, self.cr_window * self.sr_ratio, self.cr_window * self.sr_ratio, self.cr_window)

            # positional bias for carrier tokens
            ct = self.hat_pos_embed(ct)

            # attention plus mlp
            ct = ct + self.hat_drop_path(self.gamma1 * self.hat_attn(self.hat_norm1(ct)))
            ct = ct + self.hat_drop_path(self.gamma2 * self.hat_mlp(self.hat_norm2(ct)))

            # ct are put back to windows
            ct = ct_window(ct, self.cr_window * self.sr_ratio, self.cr_window * self.sr_ratio, self.cr_window)

            ct = ct.reshape(x.shape[0], -1, N)
            # concatenate carrier_tokens to the windowed tokens
            x = torch.cat((ct, x), dim=1)

        # window attention together with carrier tokens
        x = x + self.drop_path(self.gamma3 * self.attn(self.norm1(x)))
        x = x + self.drop_path(self.gamma4 * self.mlp(self.norm2(x)))

        if self.sr_ratio > 1:
            # for hierarchical attention we need to split carrier tokens and window tokens back
            ctr, x = x.split([x.shape[1] - self.window_size * self.window_size, self.window_size * self.window_size],
                             dim=1)
            ct = ctr.reshape(Bg, Ng, Hg)  # reshape carrier tokens.
            if self.last and self.do_propagation:
                # propagate carrier token information into the image
                ctr_image_space = ctr.transpose(1, 2).reshape(B, N, self.cr_window, self.cr_window)
                x = x + self.gamma1 * self.upsampler(ctr_image_space.to(dtype=torch.float32)).flatten(2).transpose(1,
                                                                                                                   2).to(
                    dtype=x.dtype)
        return x, ct


class DATB(nn.Module):
    def __init__(self, dim, num_heads, reso=64, split_size=[2, 4], shift_size=[1, 2], expansion_factor=4.,
                 qkv_bias=False, qk_scale=None, drop=0.,
                 attn_drop=0., drop_path=0., act_layer=nn.GELU, norm_layer=nn.LayerNorm, rg_idx=0, b_idx=0):
        super().__init__()

        self.norm1 = norm_layer(dim)

        if b_idx % 2 == 0:
            # DSTB
            self.attn = Adaptive_Spatial_Attention(
                dim, num_heads=num_heads, reso=reso, split_size=split_size, shift_size=shift_size, qkv_bias=qkv_bias,
                qk_scale=qk_scale,
                drop=drop, attn_drop=attn_drop, rg_idx=rg_idx, b_idx=b_idx
            )
        else:
            # DCTB
            self.attn = Adaptive_Channel_Attention(
                dim, num_heads=num_heads, qkv_bias=qkv_bias, qk_scale=qk_scale, attn_drop=attn_drop,
                proj_drop=drop
            )
        self.drop_path = DropPath(drop_path) if drop_path > 0. else nn.Identity()

        ffn_hidden_dim = int(dim * expansion_factor)
        self.ffn = SGFN(in_features=dim, hidden_features=ffn_hidden_dim, out_features=dim, act_layer=act_layer)
        self.norm2 = norm_layer(dim)

    def forward(self, x, x_size):
        """
        Input: x: (B, H*W, C), x_size: (H, W)
        Output: x: (B, H*W, C)
        """
        H, W = x_size
        x = x + self.drop_path(self.attn(self.norm1(x), H, W))
        x = x + self.drop_path(self.ffn(self.norm2(x), H, W))

        return x


class ResidualGroup(nn.Module):
    """ ResidualGroup
    Args:
        dim (int): Number of input channels.
        reso (int): Input resolution.
        num_heads (int): Number of attention heads.
        split_size (tuple(int)): Height and Width of spatial window.
        expansion_factor (float): Ratio of ffn hidden dim to embedding dim.
        qkv_bias (bool): If True, add a learnable bias to query, key, value. Default: True
        qk_scale (float | None): Override default qk scale of head_dim ** -0.5 if set. Default: None
        drop (float): Dropout rate. Default: 0
        attn_drop(float): Attention dropout rate. Default: 0
        drop_paths (float | None): Stochastic depth rate.
        act_layer (nn.Module): Activation layer. Default: nn.GELU
        norm_layer (nn.Module): Normalization layer. Default: nn.LayerNorm
        depth (int): Number of dual aggregation Transformer blocks in residual group.
        use_chk (bool): Whether to use checkpointing to save memory.
        resi_connection: The convolutional block before residual connection. '1conv'/'3conv'
    """

    def __init__(self,
                 dim,
                 reso,
                 num_heads,
                 split_size=[2, 4],
                 expansion_factor=4.,
                 qkv_bias=False,
                 qk_scale=None,
                 drop=0.,
                 attn_drop=0.,
                 drop_paths=None,
                 act_layer=nn.GELU,
                 norm_layer=nn.LayerNorm,
                 depth=2,
                 use_chk=False,
                 resi_connection='1conv',
                 rg_idx=0):
        super().__init__()
        self.use_chk = use_chk
        self.reso = reso
        self.conv_first_1 = nn.Conv2d(dim + 9, dim, 3, 1, 1)

        self.blocks = nn.ModuleList([
            DATB(
                dim=dim,
                num_heads=num_heads,
                reso=reso,
                split_size=split_size,
                shift_size=[split_size[0] // 2, split_size[1] // 2],
                expansion_factor=expansion_factor,
                qkv_bias=qkv_bias,
                qk_scale=qk_scale,
                drop=drop,
                attn_drop=attn_drop,
                drop_path=drop_paths[i],
                act_layer=act_layer,
                norm_layer=norm_layer,
                rg_idx=rg_idx,
                b_idx=i,
            ) for i in range(depth)])

        self.hat_layer = HAT(dim=dim,
                             num_heads=num_heads)

        self.global_tokenizer = TokenInitializer(dim,
                                                 224,
                                                 8,
                                                 ct_size=1)

        if resi_connection == '1conv':
            self.conv = nn.Conv2d(dim, dim, 3, 1, 1)
        elif resi_connection == '3conv':
            self.conv = nn.Sequential(
                nn.Conv2d(dim, dim // 4, 3, 1, 1), nn.LeakyReLU(negative_slope=0.2, inplace=True),
                nn.Conv2d(dim // 4, dim // 4, 1, 1, 0), nn.LeakyReLU(negative_slope=0.2, inplace=True),
                nn.Conv2d(dim // 4, dim, 3, 1, 1))

    def __pdfbdec(self, x, c, method="resize"):
        """Pyramidal directional filter bank decomposition for a batch of
        images.

        Returns a list of 4D numpy array.

        Here's an example with an image with 3 channels, and batch_size=2:
            >>> self.n_levs = [0, 3, 3, 3]
            >>> coefs, sfs = self.__pdfbdec(x)
        This will yield:
            >>> coefs[0].shape
            (2, 24, 112, 112)
            >>> coefs[1].shape
            (2, 24, 56, 56)
            >>> coefs[2].shape
            (2, 24, 28, 28)
            >>> coefs[3].shape
            (2, 12, 14, 14)
            >>> sfs.shape
            (2, 168)
        """
        # Convert to from N-D channels to single channel by averaging
        # 假设x的维度是[B, C, H, W]
        x = x.mean(dim=1, keepdim=True)  # 将通道数从C减少到1

        # Obtain coefficients
        coefs = batch_multi_channel_pdfbdec(x=x.detach(), pfilt="maxflat", dfilt="dmaxflat7", nlevs=[c], device=0)

        # Stack channels with same image dimension
        coefs = stack_same_dim(coefs)

        # Resize or splice
        if method == "resize":
            for k in coefs.keys():
                # Resize if image is not square
                if k[2] != k[3]:
                    # Get maximum dimension (height or width)
                    max_dim = int(np.max((k[2], k[3])))
                    # Resize the channels
                    trans = transforms.Compose([transforms.Resize((max_dim, max_dim))])
                    coefs[k] = trans(coefs[k])
        else:
            for k in coefs.keys():
                # Resize if image is not square
                if k[2] != k[3]:
                    # Get minimum dimension (height or width)
                    min_dim = int(np.argmin((k[2], k[3]))) + 2
                    # Splice alternate channels (always even number of channels exist)
                    coefs[k] = torch.cat((coefs[k][:, ::2, :, :], coefs[k][:, 1::2, :, :]), dim=min_dim)

        # Stack channels with same image dimension
        coefs = stack_same_dim(coefs)

        # Change coefs's key to number (n-1 to 0), instead of dimension
        for i, k in enumerate(coefs.copy()):
            idx = len(coefs.keys()) - i - 1
            coefs[idx] = coefs.pop(k)

        # Get statistical features (mean and std) for each image
        sfs = []
        for k in coefs.keys():
            sfs.append(coefs[k].mean(dim=[2, 3]))
            sfs.append(coefs[k].std(dim=[2, 3]))
        sfs = torch.cat(sfs, dim=1)

        return coefs, sfs

    def upsample_input(self, x, target_size):
        """
        上采样输入图像到目标尺寸。
        :param x: 输入图像张量，尺寸为 (B, C, H, W)。
        :param target_size: 目标尺寸，格式为 (H, W)。
        :return: 上采样后的图像张量。
        """
        return F.interpolate(x, size=target_size, mode='bilinear', align_corners=False)

    def forward(self, x, x_size):
        """
        Input: x: (B, H*W, C), x_size: (H, W)
        Output: x: (B, H*W, C)
        """
        H, W = x_size
        res = x
        for blk in self.blocks:
            if self.use_chk:
                x = checkpoint.checkpoint(blk, x, x_size)
            else:
                x = blk(x, x_size)
        x = rearrange(x, "b (h w) c -> b c h w", h=H, w=W)

        coefs, _ = self.__pdfbdec(x, 3, method="resize")
        if len(coefs) == 1:
            # 如果coefs只有一个元素，直接上采样并赋值
            counterlet_features = self.upsample_input(coefs[0], (x.shape[2], x.shape[3]))
        else:
            # 如果coefs有多个元素，初始化一个空列表来收集所有上采样后的特征
            upsampled_features = []
            for i in range(len(coefs)):  # 注意这里的修改，使用range来遍历索引
                # 对每个coefs[i]进行上采样
                upsampled_feature = self.upsample_input(coefs[i], (x.shape[2], x.shape[3]))
                upsampled_features.append(upsampled_feature)
            # 使用torch.cat在通道维度上拼接所有上采样后的特征
            counterlet_features = torch.cat(upsampled_features, dim=1)

        x_ccnn = torch.cat((x, counterlet_features.cuda(0)), 1)
        x_ccnn = self.conv_first_1(x_ccnn)

        ct = self.global_tokenizer(x)
        B, C, H, W = x.shape
        x_hat = window_partition(x, 8)
        x_hat, ct = self.hat_layer(x_hat, ct)
        x_hat = window_reverse(x_hat, 8, H, W, B)
        x_gate = x_ccnn * x_hat
        x = self.conv(x_gate)
        x = x + x_ccnn
        x = rearrange(x, "b c h w -> b (h w) c")
        x = res + x

        return x


class Upsample(nn.Sequential):
    """Upsample module.
    Args:
        scale (int): Scale factor. Supported scales: 2^n and 3.
        num_feat (int): Channel number of intermediate features.
    """

    def __init__(self, scale, num_feat):
        m = []
        if (scale & (scale - 1)) == 0:  # scale = 2^n
            for _ in range(int(math.log(scale, 2))):
                m.append(nn.Conv2d(num_feat, 4 * num_feat, 3, 1, 1))
                m.append(nn.PixelShuffle(2))
        elif scale == 3:
            m.append(nn.Conv2d(num_feat, 9 * num_feat, 3, 1, 1))
            m.append(nn.PixelShuffle(3))
        else:
            raise ValueError(f'scale {scale} is not supported. ' 'Supported scales: 2^n and 3.')
        super(Upsample, self).__init__(*m)


class UpsampleOneStep(nn.Sequential):
    """UpsampleOneStep module (the difference with Upsample is that it always only has 1conv + 1pixelshuffle)
       Used in lightweight SR to save parameters.

    Args:
        scale (int): Scale factor. Supported scales: 2^n and 3.
        num_feat (int): Channel number of intermediate features.

    """

    def __init__(self, scale, num_feat, num_out_ch, input_resolution=None):
        self.num_feat = num_feat
        self.input_resolution = input_resolution
        m = []
        m.append(nn.Conv2d(num_feat, (scale ** 2) * num_out_ch, 3, 1, 1))
        m.append(nn.PixelShuffle(scale))
        super(UpsampleOneStep, self).__init__(*m)

    def flops(self):
        h, w = self.input_resolution
        flops = h * w * self.num_feat * 3 * 9
        return flops


class AdaptationLayer(nn.Module):
    def __init__(self, in_channels, out_channels, output_size):
        super(AdaptationLayer, self).__init__()
        self.conv = nn.Conv2d(in_channels, out_channels, kernel_size=1, stride=1, padding=0)
        self.output_size = output_size

    def forward(self, x):
        x = self.conv(x)
        x = F.interpolate(x, size=self.output_size, mode='bilinear', align_corners=False)
        return x


@ARCH_REGISTRY.register()
class DAT(nn.Module):
    """ Dual Aggregation Transformer
    Args:
        img_size (int): Input image size. Default: 64
        in_chans (int): Number of input image channels. Default: 3
        embed_dim (int): Patch embedding dimension. Default: 180
        depths (tuple(int)): Depth of each residual group (number of DATB in each RG).
        split_size (tuple(int)): Height and Width of spatial window.
        num_heads (tuple(int)): Number of attention heads in different residual groups.
        expansion_factor (float): Ratio of ffn hidden dim to embedding dim. Default: 4
        qkv_bias (bool): If True, add a learnable bias to query, key, value. Default: True
        qk_scale (float | None): Override default qk scale of head_dim ** -0.5 if set. Default: None
        drop_rate (float): Dropout rate. Default: 0
        attn_drop_rate (float): Attention dropout rate. Default: 0
        drop_path_rate (float): Stochastic depth rate. Default: 0.1
        act_layer (nn.Module): Activation layer. Default: nn.GELU
        norm_layer (nn.Module): Normalization layer. Default: nn.LayerNorm
        use_chk (bool): Whether to use checkpointing to save memory.
        upscale: Upscale factor. 2/3/4 for image SR
        img_range: Image range. 1. or 255.
        resi_connection: The convolutional block before residual connection. '1conv'/'3conv'
    """

    def __init__(self,
                 img_size=64,
                 in_chans=3,
                 embed_dim=180,
                 split_size=[2, 4],
                 depth=[2, 2, 2, 2],
                 num_heads=[2, 2, 2, 2],
                 expansion_factor=4.,
                 qkv_bias=True,
                 qk_scale=None,
                 drop_rate=0.,
                 attn_drop_rate=0.,
                 drop_path_rate=0.1,
                 act_layer=nn.GELU,
                 norm_layer=nn.LayerNorm,
                 use_chk=False,
                 upscale=2,
                 img_range=1.,
                 resi_connection='1conv',
                 upsampler='pixelshuffle',
                 **kwargs):
        super().__init__()

        num_in_ch = in_chans
        num_out_ch = in_chans
        num_feat = 64
        self.img_range = img_range
        if in_chans == 3:
            rgb_mean = (0.4488, 0.4371, 0.4040)
            self.mean = torch.Tensor(rgb_mean).view(1, 3, 1, 1)
        else:
            self.mean = torch.zeros(1, 1, 1, 1)
        self.upscale = upscale
        self.upsampler = upsampler

        # ------------------------- 1, Shallow Feature Extraction ------------------------- #

        self.conv_first = nn.Conv2d(num_in_ch, embed_dim, 3, 1, 1)
        self.conv_first_1 = nn.Conv2d(embed_dim + 9, embed_dim, 3, 1, 1)

        self.adaptation_layer = AdaptationLayer(in_channels=256, out_channels=180, output_size=(64, 64))

        # ------------------------- 2, Deep Feature Extraction ------------------------- #
        self.num_layers = len(depth)
        self.use_chk = use_chk
        self.num_features = self.embed_dim = embed_dim  # num_features for consistency with other models
        heads = num_heads

        self.before_RG = nn.Sequential(
            Rearrange('b c h w -> b (h w) c'),
            nn.LayerNorm(embed_dim)
        )

        curr_dim = embed_dim
        dpr = [x.item() for x in torch.linspace(0, drop_path_rate, np.sum(depth))]  # stochastic depth decay rule

        self.layers = nn.ModuleList()
        for i in range(self.num_layers):
            layer = ResidualGroup(
                dim=embed_dim,
                num_heads=heads[i],
                reso=img_size,
                split_size=split_size,
                expansion_factor=expansion_factor,
                qkv_bias=qkv_bias,
                qk_scale=qk_scale,
                drop=drop_rate,
                attn_drop=attn_drop_rate,
                drop_paths=dpr[sum(depth[:i]):sum(depth[:i + 1])],
                act_layer=act_layer,
                norm_layer=norm_layer,
                depth=depth[i],
                use_chk=use_chk,
                resi_connection=resi_connection,
                rg_idx=i)
            self.layers.append(layer)

        self.norm = norm_layer(curr_dim)
        # build the last conv layer in deep feature extraction
        if resi_connection == '1conv':
            self.conv_after_body = nn.Conv2d(embed_dim, embed_dim, 3, 1, 1)
        elif resi_connection == '3conv':
            # to save parameters and memory
            self.conv_after_body = nn.Sequential(
                nn.Conv2d(embed_dim, embed_dim // 4, 3, 1, 1), nn.LeakyReLU(negative_slope=0.2, inplace=True),
                nn.Conv2d(embed_dim // 4, embed_dim // 4, 1, 1, 0), nn.LeakyReLU(negative_slope=0.2, inplace=True),
                nn.Conv2d(embed_dim // 4, embed_dim, 3, 1, 1))

        # ------------------------- 3, Reconstruction ------------------------- #
        if self.upsampler == 'pixelshuffle':
            # for classical SR
            self.conv_before_upsample = nn.Sequential(
                nn.Conv2d(embed_dim, num_feat, 3, 1, 1), nn.LeakyReLU(inplace=True))
            self.upsample = Upsample(upscale, num_feat)
            self.conv_last = nn.Conv2d(num_feat, num_out_ch, 3, 1, 1)
        elif self.upsampler == 'pixelshuffledirect':
            # for lightweight SR (to save parameters)
            self.upsample = UpsampleOneStep(upscale, embed_dim, num_out_ch,
                                            (img_size, img_size))

        self.apply(self._init_weights)

    def upsample_input(self, x, target_size):
        """
        上采样输入图像到目标尺寸。
        :param x: 输入图像张量，尺寸为 (B, C, H, W)。
        :param target_size: 目标尺寸，格式为 (H, W)。
        :return: 上采样后的图像张量。
        """
        return F.interpolate(x, size=target_size, mode='bilinear', align_corners=False)

    def _init_weights(self, m):
        if isinstance(m, nn.Linear):
            trunc_normal_(m.weight, std=.02)
            if isinstance(m, nn.Linear) and m.bias is not None:
                nn.init.constant_(m.bias, 0)
        elif isinstance(m, (nn.LayerNorm, nn.BatchNorm2d, nn.GroupNorm, nn.InstanceNorm2d)):
            nn.init.constant_(m.bias, 0)
            nn.init.constant_(m.weight, 1.0)

    def forward_features(self, x):
        _, _, H, W = x.shape
        x_size = [H, W]
        x = self.before_RG(x)
        for layer in self.layers:
            x = layer(x, x_size)
        x = self.norm(x)
        x = rearrange(x, "b (h w) c -> b c h w", h=H, w=W)

        return x

    def __pdfbdec(self, x, method="resize"):
        """Pyramidal directional filter bank decomposition for a batch of
        images.

        Returns a list of 4D numpy array.

        Here's an example with an image with 3 channels, and batch_size=2:
            >>> self.n_levs = [0, 3, 3, 3]
            >>> coefs, sfs = self.__pdfbdec(x)
        This will yield:
            >>> coefs[0].shape
            (2, 24, 112, 112)
            >>> coefs[1].shape
            (2, 24, 56, 56)
            >>> coefs[2].shape
            (2, 24, 28, 28)
            >>> coefs[3].shape
            (2, 12, 14, 14)
            >>> sfs.shape
            (2, 168)
        """
        # Convert to from N-D channels to single channel by averaging
        imgs = []
        # Iterate each image in a batch
        for i in range(x.shape[0]):
            # Convert to PIL and image and to grayscale image
            img = transforms.ToPILImage()(x[i])
            img = to_grayscale(img)
            imgs.append(img)
        # Restack and convert back to PyTorch tensor
        x = torch.from_numpy((np.expand_dims(np.stack(imgs, axis=0), axis=1)))

        # Obtain coefficients
        coefs = batch_multi_channel_pdfbdec(x=x, pfilt="maxflat", dfilt="dmaxflat7", nlevs=[3], device=0)
        # coefs = batch_multi_channel_pdfbdec(x=x, pfilt="maxflat", dfilt="dmaxflat7", nlevs=[0, 2, 2, 2],
        #                                     device=self.device)

        # Stack channels with same image dimension
        coefs = stack_same_dim(coefs)

        # Resize or splice
        if method == "resize":
            for k in coefs.keys():
                # Resize if image is not square
                if k[2] != k[3]:
                    # Get maximum dimension (height or width)
                    max_dim = int(np.max((k[2], k[3])))
                    # Resize the channels
                    trans = transforms.Compose([transforms.Resize((max_dim, max_dim))])
                    coefs[k] = trans(coefs[k])
        else:
            for k in coefs.keys():
                # Resize if image is not square
                if k[2] != k[3]:
                    # Get minimum dimension (height or width)
                    min_dim = int(np.argmin((k[2], k[3]))) + 2
                    # Splice alternate channels (always even number of channels exist)
                    coefs[k] = torch.cat((coefs[k][:, ::2, :, :], coefs[k][:, 1::2, :, :]), dim=min_dim)

        # Stack channels with same image dimension
        coefs = stack_same_dim(coefs)

        # Change coefs's key to number (n-1 to 0), instead of dimension
        for i, k in enumerate(coefs.copy()):
            idx = len(coefs.keys()) - i - 1
            coefs[idx] = coefs.pop(k)

        # Get statistical features (mean and std) for each image
        sfs = []
        for k in coefs.keys():
            sfs.append(coefs[k].mean(dim=[2, 3]))
            sfs.append(coefs[k].std(dim=[2, 3]))
        sfs = torch.cat(sfs, dim=1)

        return coefs, sfs

    def forward(self, x):
        """
        Input: x: (B, C, H, W)
        """
        self.mean = self.mean.type_as(x)
        x = (x - self.mean) * self.img_range
        # print('input shape:', x.shape)

        coefs, _ = self.__pdfbdec(x, method="resize")

        if len(coefs) == 1:
            # 如果coefs只有一个元素，直接上采样并赋值
            counterlet_features = self.upsample_input(coefs[0], (x.shape[2], x.shape[3]))
        else:
            # 如果coefs有多个元素，初始化一个空列表来收集所有上采样后的特征
            upsampled_features = []
            for i in range(len(coefs)):  # 注意这里的修改，使用range来遍历索引
                # 对每个coefs[i]进行上采样
                upsampled_feature = self.upsample_input(coefs[i], (x.shape[2], x.shape[3]))
                upsampled_features.append(upsampled_feature)
            # 使用torch.cat在通道维度上拼接所有上采样后的特征
            counterlet_features = torch.cat(upsampled_features, dim=1)

        if self.upsampler == 'pixelshuffle':
            # for image SR
            # x = self.upsample_input(x, (224, 224))
            x = self.conv_first(x)
            x = self.conv_after_body(self.forward_features(x)) + x
            x = self.conv_before_upsample(x)
            x = self.conv_last(self.upsample(x))
        elif self.upsampler == 'pixelshuffledirect':
            x = self.conv_first(x)
            x = self.conv_after_body(self.forward_features(x)) + x
            x = self.upsample(x)

        x = x / self.img_range + self.mean
        return x


if __name__ == '__main__':
    upscale = 1
    height = 64
    width = 64
    model = DAT(
        upscale=2,
        in_chans=3,
        img_size=64,
        img_range=1.,
        depth=[6, 6, 6, 6, 6, 6],
        embed_dim=180,
        num_heads=[6, 6, 6, 6, 6, 6],
        expansion_factor=2,
        resi_connection='1conv',
        split_size=[8, 16],
    ).cuda().eval()

    print(height, width)

    x = torch.randn((1, 3, height, width)).cuda()
    x = model(x)

    print(x.shape)