from tkinter.tix import Tree

import numpy as np

from disentangle.configs.default_config import get_default_config
from disentangle.core.data_type import DataType
from disentangle.core.loss_type import LossType
from disentangle.core.model_type import ModelType
from disentangle.core.sampler_type import SamplerType
from disentangle.data_loader.pavia2_enums import Pavia2DataSetChannels


def get_config():
    config = get_default_config()
    data = config.data
    data.image_size = 64
    data.data_type = DataType.Pavia2VanillaSplitting
    data.channel_1 = Pavia2DataSetChannels.NucRFP670
    data.channel_2 = Pavia2DataSetChannels.TUBULIN
    data.ch1_min_alpha = 0.48
    data.ch1_max_alpha = 0.52
    # data.cl_std_filter = [30.0, 30.0]
    data.ch1_alpha_interval_count = 20
    data.channel_2_downscale_factor = 1

    data.sampler_type = SamplerType.DefaultGridSampler
    data.deterministic_grid = True
    data.normalized_input = True
    data.clip_percentile = 0.995
    # If this is set to true, then one mean and stdev is used for both channels. Otherwise, two different
    # meean and stdev are used.
    data.use_one_mu_std = True
    data.train_aug_rotate = False
    data.randomized_channels = False
    data.multiscale_lowres_count = None
    data.padding_mode = 'reflect'
    data.padding_value = None
    # If this is set to True, then target channels will be normalized from their separate mean.
    # otherwise, target will be normalized just the same way as the input, which is determined by use_one_mu_std
    data.target_separate_normalization = False
    data.return_individual_channels = True
    data.return_alpha = True
    data.use_alpha_invariant_mean = True

    loss = config.loss
    loss.loss_type = LossType.ElboCL
    loss.cl_tau_pos = 0.0
    loss.cl_tau_neg = 0.5
    loss.cl_weight = 0.0
    # loss.mixed_rec_weight = 1

    loss.kl_weight = 1
    loss.kl_annealing = False
    loss.kl_annealtime = 10
    loss.kl_start = -1
    loss.kl_min = 1e-7
    loss.free_bits = 0.0
    loss.skip_cl_on_alpha = True
    loss.enable_alpha_weighted_loss = False

    model = config.model
    model.model_type = ModelType.LadderVaeTwinDecoder
    model.z_dims = [128, 128, 128, 128]

    model.encoder.batchnorm = False
    model.encoder.blocks_per_layer = 3
    model.encoder.n_filters = 64
    model.encoder.dropout = 0.1
    model.encoder.res_block_kernel = 3
    model.encoder.res_block_skip_padding = False

    model.decoder.batchnorm = False
    model.decoder.blocks_per_layer = 1
    model.decoder.n_filters = 64
    model.decoder.dropout = 0.1
    model.decoder.res_block_kernel = 3
    model.decoder.res_block_skip_padding = False
    model.decoder.multiscale_retain_spatial_dims = True
    model.decoder.conv2d_bias = False

    model.skip_nboundary_pixels_from_loss = None
    model.nonlin = 'leakyrelu'
    model.merge_type = 'residual_ungated'
    model.stochastic_skip = True
    model.learn_top_prior = True
    model.img_shape = None
    model.res_block_type = 'bacdbacd'
    model.gated = False
    model.no_initial_downscaling = True
    model.analytical_kl = False
    model.mode_pred = False
    model.var_clip_max = 20
    # predict_logvar takes one of the four values: [None,'global','channelwise','pixelwise']
    model.predict_logvar = None
    model.logvar_lowerbound = -5  # -2.49 is log(1/12), from paper "Re-parametrizing VAE for stablity."
    model.multiscale_lowres_separate_branch = False
    model.multiscale_retain_spatial_dims = True
    model.monitor = 'val_psnr'  # {'val_loss','val_psnr'}
    model.non_stochastic_version = True
    model.cl_latent_start_end_alpha = (0, 0)
    diff = model.z_dims[0] - model.cl_latent_start_end_alpha[1]
    model.cl_latent_start_end_ch1 = (model.cl_latent_start_end_alpha[1], model.cl_latent_start_end_alpha[1] + diff // 2)
    model.cl_latent_start_end_ch2 = (model.cl_latent_start_end_ch1[1], model.z_dims[0])
    # This is actually, a data normalization which is unfortunately happening inside the model.
    model.cl_enable_summed_target_equality = True

    training = config.training
    training.lr = 0.001
    training.lr_scheduler_patience = 15
    training.max_epochs = 200
    training.batch_size = 48
    training.num_workers = 4
    training.val_repeat_factor = None
    training.train_repeat_factor = None
    training.val_fraction = 0.1
    training.test_fraction = 0.1
    training.earlystop_patience = 100
    training.precision = 16

    return config
