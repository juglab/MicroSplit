import torch
import torch.nn as nn

from disentangle.nets.positional_encoding import TwoDimPositionalEncoder
from mamba_ssm import Mamba


class UMambaBlock(nn.Module):
    """
    Note: Mamba returns the y, and not the state. That being the case, we need to make sure that if we want to 
    pick a subset of the output, we should pick the last k and not the first k.
    """

    def __init__(self,
                 in_channels,
                 ssm_expansion_factor,
                 conv1d_kernel_size,
                 state_dim,
                 starting_conv_blocks=2,
                 enable_positional_encoding=False):
        super().__init__()
        self._in_c = in_channels
        self._ssm_exp = ssm_expansion_factor
        self._ssm_conv1d_k = conv1d_kernel_size
        self._ssm_state_dim = state_dim
        self._conv_blocks = self.get_conv_blocks(starting_conv_blocks)
        self._enable_positional_encoding = enable_positional_encoding
        self._pos_encoder = None
        self._last_channel_matcher = nn.Identity()
        self._pos_encoder_emb_dim = 0
        if self._enable_positional_encoding:
            self._pos_encoder_emb_dim = 16
            self._pos_encoder = TwoDimPositionalEncoder(emb_dim=self._pos_encoder_emb_dim, max_x=64, max_y=64)
            self._last_channel_matcher = nn.Conv2d(self._in_c + self._pos_encoder_emb_dim, self._in_c, 1)

        self._ssm = Mamba(d_model=self._in_c + self._pos_encoder_emb_dim,
                          expand=self._ssm_exp,
                          d_conv=self._ssm_conv1d_k,
                          d_state=self._ssm_state_dim)

    def get_conv_blocks(self, starting_conv_blocks):
        modules = []
        for _ in range(starting_conv_blocks):
            modules.append(nn.Conv2d(self._in_c, self._in_c, 3, padding=1))
            modules.append(nn.LeakyReLU())
        return nn.Sequential(*modules)

    def forward(self, x):
        # at this point, x is of shape (batch_size, in_channels, h, w)
        bN, cN, hN, wN = x.shape

        x = self._conv_blocks(x)
        if self._enable_positional_encoding:
            x = torch.cat([x, self._pos_encoder(0, hN, 0, wN)], dim=1)
        # we want to flatten the last two dimensions and swap the last two dimensions
        # so that we can apply the ssm
        x = x.flatten(start_dim=2).permute(0, 2, 1)
        x = self._ssm(x)
        # at this point, x is of shape (batch_size, h*w, in_channels)
        # we want to swap the last two dimensions and reshape the last dimension
        # so that we can apply the convolutions
        x = x.permute(0, 2, 1).reshape(bN, cN, hN, wN)
        x = self._last_channel_matcher(x)
        return x


class ConditionalMamba(UMambaBlock):

    def __init__(self,
                 in_channels,
                 ssm_expansion_factor,
                 conv1d_kernel_size,
                 state_dim,
                 primary_first=False,
                 starting_conv_blocks=2,
                 enable_positional_encoding=False):
        super().__init__(in_channels,
                         ssm_expansion_factor,
                         conv1d_kernel_size,
                         state_dim,
                         starting_conv_blocks=starting_conv_blocks,
                         enable_positional_encoding=enable_positional_encoding)

        self._conv_blocks_primary = self.get_conv_blocks(starting_conv_blocks)
        self._primary_first = primary_first
        print(
            f'[{self.__class__.__name__}] {in_channels} {ssm_expansion_factor} {state_dim} primary_first: {self._primary_first} pos_enc:{enable_positional_encoding}'
        )

    def forward(self, primary_x, conditional_x):
        # bN, cN, hN, wN = primary_x.shape
        conditional_x = self._conv_blocks(conditional_x)
        primary_x = self._conv_blocks_primary(primary_x)

        if self._enable_positional_encoding:
            _, _, hN, wN = primary_x.shape
            _, _, hcond_N, wcond_N = conditional_x.shape

            assert abs((hcond_N - hN) % 2) == 0
            assert abs((wcond_N - wN) % 2) == 0
            x_idx_begin = abs((hcond_N - hN) // 2)
            y_idx_begin = abs((wcond_N - wN) // 2)
            if hcond_N > hN:
                primary_pos = self._pos_encoder(x_idx_begin, hcond_N - x_idx_begin, y_idx_begin, wcond_N - y_idx_begin)
                conditional_pos = self._pos_encoder(0, hcond_N, 0, wcond_N)
            else:
                primary_pos = self._pos_encoder(0, hN, 0, wN)
                conditional_pos = self._pos_encoder(x_idx_begin, hN - x_idx_begin, y_idx_begin, wN - y_idx_begin)

            conditional_pos = conditional_pos.repeat(primary_x.shape[0], 1, 1, 1)
            primary_pos = primary_pos.repeat(conditional_x.shape[0], 1, 1, 1)

            primary_x = torch.cat([primary_x, primary_pos], dim=1)
            conditional_x = torch.cat([conditional_x, conditional_pos], dim=1)

        shape = primary_x.shape
        # we want to flatten the last two dimensions and swap the last two dimensions
        # so that we can apply the ssm
        conditional_x = conditional_x.flatten(start_dim=2).permute(0, 2, 1)
        primary_x = primary_x.flatten(start_dim=2).permute(0, 2, 1)
        # The order is important here.
        if self._primary_first:
            x = torch.cat((primary_x, conditional_x), dim=1)
        else:
            x = torch.cat((conditional_x, primary_x), dim=1)

        x = self._ssm(x)
        x = x[:, -primary_x.shape[1]:]

        # at this point, x is of shape (batch_size, h*w, in_channels)
        # we want to swap the last two dimensions and reshape the last dimension
        # so that we can apply the convolutions
        x = x.permute(0, 2, 1).reshape(shape)
        x = self._last_channel_matcher(x)
        return x


if __name__ == "__main__":
    import torch

    # x = torch.randn(1, 64, 32, 32).cuda()
    # mamba_block = UMambaBlock(in_channels=64, ssm_expansion_factor=2, conv1d_kernel_size=4, state_dim=64)
    # mamba_block = mamba_block.cuda()
    # print(mamba_block)
    # y = mamba_block(x)
    # print(x.shape)
    # print(y.shape)

    conditional_x = torch.randn(1, 64, 64, 64).cuda()
    primary_x = torch.randn(1, 64, 8, 8).cuda()
    mamba_block = ConditionalMamba(in_channels=64,
                                   ssm_expansion_factor=2,
                                   conv1d_kernel_size=4,
                                   state_dim=64,
                                   enable_positional_encoding=True)
    mamba_block = mamba_block.cuda()
    print(mamba_block)
    y = mamba_block(primary_x, conditional_x)
    print(primary_x.shape)
    print(y.shape)
