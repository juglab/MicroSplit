import argparse
import glob
import os
import pickle
import random
import re
import sys
from copy import deepcopy
from posixpath import basename

import matplotlib.pyplot as plt
import numpy as np
import torch
import torch.nn as nn
from skimage.metrics import structural_similarity
from torch.utils.data import DataLoader
from tqdm import tqdm

import ml_collections
from disentangle.analysis.critic_notebook_utils import get_label_separated_loss, get_mmse_dict
from disentangle.analysis.lvae_utils import get_img_from_forward_output
from disentangle.analysis.mmse_prediction import get_dset_predictions
from disentangle.analysis.plot_utils import clean_ax, get_k_largest_indices, plot_imgs_from_idx
from disentangle.analysis.results_handler import PaperResultsHandler
from disentangle.analysis.stitch_prediction import stitch_predictions
from disentangle.config_utils import load_config
from disentangle.core.data_split_type import DataSplitType, get_datasplit_tuples
from disentangle.core.data_type import DataType
from disentangle.core.loss_type import LossType
from disentangle.core.model_type import ModelType
from disentangle.core.psnr import PSNR, RangeInvariantPsnr
from disentangle.core.tiff_reader import load_tiff
from disentangle.data_loader.lc_multich_dloader import LCMultiChDloader
from disentangle.data_loader.patch_index_manager import GridAlignement
# from disentangle.data_loader.two_tiff_rawdata_loader import get_train_val_data
from disentangle.data_loader.vanilla_dloader import MultiChDloader, get_train_val_data
from disentangle.sampler.random_sampler import RandomSampler
from disentangle.training import create_dataset, create_model

# from disentangle.data_loader.single_channel_dloader import SingleChannelDloader

torch.multiprocessing.set_sharing_strategy('file_system')
DATA_ROOT = 'PUT THE ROOT DIRECTORY FOR THE DATASET HERE'
CODE_ROOT = 'PUT THE ROOT DIRECTORY FOR THE CODE HERE'


def _avg_psnr(target, prediction, psnr_fn):
    output = np.mean([psnr_fn(target[i:i + 1], prediction[i:i + 1]).item() for i in range(len(prediction))])
    return round(output, 2)


def avg_range_inv_psnr(target, prediction):
    return _avg_psnr(target, prediction, RangeInvariantPsnr)


def avg_psnr(target, prediction):
    return _avg_psnr(target, prediction, PSNR)


def compute_masked_psnr(mask, tar1, tar2, pred1, pred2):
    mask = mask.astype(bool)
    mask = mask[..., 0]
    tmp_tar1 = tar1[mask].reshape((len(tar1), -1, 1))
    tmp_pred1 = pred1[mask].reshape((len(tar1), -1, 1))
    tmp_tar2 = tar2[mask].reshape((len(tar2), -1, 1))
    tmp_pred2 = pred2[mask].reshape((len(tar2), -1, 1))
    psnr1 = avg_range_inv_psnr(tmp_tar1, tmp_pred1)
    psnr2 = avg_range_inv_psnr(tmp_tar2, tmp_pred2)
    return psnr1, psnr2


def avg_ssim(target, prediction):
    ssim = [
        structural_similarity(target[i], prediction[i], data_range=target[i].max() - target[i].min())
        for i in range(len(target))
    ]
    return np.mean(ssim), np.std(ssim)


def fix_seeds():
    torch.manual_seed(0)
    torch.cuda.manual_seed(0)
    np.random.seed(0)
    random.seed(0)
    torch.backends.cudnn.deterministic = True


def upperclip_data(data, max_val):
    """
    data: (N, H, W, C)
    """
    if isinstance(max_val, list):
        chN = data.shape[-1]
        assert chN == len(max_val)
        for ch in range(chN):
            ch_data = data[..., ch]
            ch_q = max_val[ch]
            ch_data[ch_data > ch_q] = ch_q
            data[..., ch] = ch_data
    else:
        data[data > max_val] = max_val
    return True


def compute_max_val(data, config):
    if config.data.get('channelwise_quantile', False):
        max_val_arr = [np.quantile(data[..., i], config.data.clip_percentile) for i in range(data.shape[-1])]
        return max_val_arr
    else:
        return np.quantile(data, config.data.clip_percentile)


def compute_high_snr_stats(config, highres_data, pred_unnorm):
    assert config.model.model_type == ModelType.DenoiserSplitter or config.data.data_type == DataType.SeparateTiffData
    psnr1 = avg_range_inv_psnr(highres_data[..., 0], pred_unnorm[0])
    psnr2 = avg_range_inv_psnr(highres_data[..., 1], pred_unnorm[1])

    ssim1_hres_mean, ssim1_hres_std = avg_ssim(highres_data[..., 0], pred_unnorm[0])
    ssim2_hres_mean, ssim2_hres_std = avg_ssim(highres_data[..., 1], pred_unnorm[1])
    print('PSNR on Highres', psnr1, psnr2)
    print('SSIM on Highres', np.round(ssim1_hres_mean, 3), '±', np.round(ssim1_hres_std, 3),
          np.round(ssim2_hres_mean, 3), '±', np.round(ssim2_hres_std, 3))
    return {'psnr': [psnr1, psnr2], 'ssim': [ssim1_hres_mean, ssim2_hres_mean, ssim1_hres_std, ssim2_hres_std]}


def get_data_without_synthetic_noise(data_dir, config, eval_datasplit_type):
    """
    Here, we don't add any synthetic noise.
    """
    assert 'synthetic_gaussian_scale' in config.data or 'poisson_noise_factor' in config.data
    assert config.data.synthetic_gaussian_scale > 0
    data_config = deepcopy(config.data)
    data_config.poisson_noise_factor = -1
    data_config.synthetic_gaussian_scale = None
    highres_data = get_train_val_data(data_config, data_dir, DataSplitType.Train, config.training.val_fraction,
                                      config.training.test_fraction)

    hres_max_val = compute_max_val(highres_data, config)
    del highres_data

    highres_data = get_train_val_data(data_config, data_dir, eval_datasplit_type, config.training.val_fraction,
                                      config.training.test_fraction)

    # highres_data = highres_data[::5].copy()
    upperclip_data(highres_data, hres_max_val)
    return highres_data


def get_highres_data_ventura(data_dir, config, eval_datasplit_type):
    data_config = ml_collections.ConfigDict()
    data_config.ch1_fname = 'actin-60x-noise2-highsnr.tif'
    data_config.ch2_fname = 'mito-60x-noise2-highsnr.tif'
    data_config.data_type = DataType.SeparateTiffData
    highres_data = get_train_val_data(data_config, data_dir, DataSplitType.Train, config.training.val_fraction,
                                      config.training.test_fraction)

    hres_max_val = compute_max_val(highres_data, config)
    del highres_data

    highres_data = get_train_val_data(data_config, data_dir, eval_datasplit_type, config.training.val_fraction,
                                      config.training.test_fraction)

    # highres_data = highres_data[::5].copy()
    upperclip_data(highres_data, hres_max_val)
    return highres_data


def main(
    ckpt_dir,
    DEBUG,
    image_size_for_grid_centers=64,
    mmse_count=1,
    custom_image_size=64,
    batch_size=16,
    num_workers=4,
    COMPUTE_LOSS=False,
    use_deterministic_grid=None,
    threshold=None,  # 0.02,
    compute_kl_loss=False,
    evaluate_train=False,
    eval_datasplit_type=DataSplitType.Val,
    val_repeat_factor=None,
    psnr_type='range_invariant',
    ignored_last_pixels=0,
    ignore_first_pixels=0,
    print_token='',
    normalized_ssim=True,
    save_to_file=False,
):
    global DATA_ROOT, CODE_ROOT

    homedir = os.path.expanduser('~')
    nodename = os.uname().nodename

    if nodename == 'capablerutherford-02aa4':
        DATA_ROOT = '/mnt/ashesh/'
        CODE_ROOT = '/home/ubuntu/ashesh/'
    elif nodename in ['capableturing-34a32', 'colorfuljug-fa782', 'agileschroedinger-a9b1c', 'rapidkepler-ca36f']:
        DATA_ROOT = '/home/ubuntu/ashesh/data/'
        CODE_ROOT = '/home/ubuntu/ashesh/'
    elif (re.match('lin-jug-\d{2}', nodename) or re.match('gnode\d{2}', nodename)
          or re.match('lin-jug-m-\d{2}', nodename) or re.match('lin-jug-l-\d{2}', nodename)):
        DATA_ROOT = '/group/jug/ashesh/data/'
        CODE_ROOT = '/home/ashesh.ashesh/'

    dtype = int(ckpt_dir.split('/')[-2].split('-')[0][1:])

    if DEBUG:
        if dtype == DataType.CustomSinosoid:
            data_dir = f'{DATA_ROOT}/sinosoid/'
        elif dtype == DataType.OptiMEM100_014:
            data_dir = f'{DATA_ROOT}/microscopy/'
    else:
        if dtype == DataType.CustomSinosoid:
            data_dir = f'{DATA_ROOT}/sinosoid/'
        elif dtype == DataType.CustomSinosoidThreeCurve:
            data_dir = f'{DATA_ROOT}/sinosoid/'
        elif dtype == DataType.OptiMEM100_014:
            data_dir = f'{DATA_ROOT}/microscopy/'
        elif dtype == DataType.Prevedel_EMBL:
            data_dir = f'{DATA_ROOT}/Prevedel_EMBL/PKG_3P_dualcolor_stacks/NoAverage_NoRegistration/'
        elif dtype == DataType.AllenCellMito:
            data_dir = f'{DATA_ROOT}/allencell/2017_03_08_Struct_First_Pass_Seg/AICS-11/'
        elif dtype == DataType.SeparateTiffData:
            data_dir = f'{DATA_ROOT}/ventura_gigascience'
        elif dtype == DataType.BioSR_MRC:
            data_dir = f'{DATA_ROOT}/BioSR/'

    homedir = os.path.expanduser('~')
    nodename = os.uname().nodename

    def get_best_checkpoint(ckpt_dir):
        output = []
        for filename in glob.glob(ckpt_dir + "/*_best.ckpt"):
            output.append(filename)
        assert len(output) == 1, '\n'.join(output)
        return output[0]

    config = load_config(ckpt_dir)
    config = ml_collections.ConfigDict(config)
    old_image_size = None
    with config.unlocked():
        try:
            if 'batchnorm' not in config.model.encoder:
                config.model.encoder.batchnorm = config.model.batchnorm
                assert 'batchnorm' not in config.model.decoder
                config.model.decoder.batchnorm = config.model.batchnorm

            if 'conv2d_bias' not in config.model.decoder:
                config.model.decoder.conv2d_bias = True

            if config.model.model_type == ModelType.LadderVaeSepEncoder:
                if 'use_random_for_missing_inp' not in config.model:
                    config.model.use_random_for_missing_inp = False
                if 'learnable_merge_tensors' not in config.model:
                    config.model.learnable_merge_tensors = False

            if 'input_is_sum' not in config.data:
                config.data.input_is_sum = False
        except:
            pass

        if config.model.model_type == ModelType.UNet and 'n_levels' not in config.model:
            config.model.n_levels = 4
        if 'test_fraction' not in config.training:
            config.training.test_fraction = 0.0

        if 'datadir' not in config:
            config.datadir = ''
        if 'encoder' not in config.model:
            config.model.encoder = ml_collections.ConfigDict()
            assert 'decoder' not in config.model
            config.model.decoder = ml_collections.ConfigDict()

            config.model.encoder.dropout = config.model.dropout
            config.model.decoder.dropout = config.model.dropout
            config.model.encoder.blocks_per_layer = config.model.blocks_per_layer
            config.model.decoder.blocks_per_layer = config.model.blocks_per_layer
            config.model.encoder.n_filters = config.model.n_filters
            config.model.decoder.n_filters = config.model.n_filters

        if 'multiscale_retain_spatial_dims' not in config.model:
            config.multiscale_retain_spatial_dims = False

        if 'res_block_kernel' not in config.model.encoder:
            config.model.encoder.res_block_kernel = 3
            assert 'res_block_kernel' not in config.model.decoder
            config.model.decoder.res_block_kernel = 3

        if 'res_block_skip_padding' not in config.model.encoder:
            config.model.encoder.res_block_skip_padding = False
            assert 'res_block_skip_padding' not in config.model.decoder
            config.model.decoder.res_block_skip_padding = False

        if config.data.data_type == DataType.CustomSinosoid:
            if 'max_vshift_factor' not in config.data:
                config.data.max_vshift_factor = config.data.max_shift_factor
                config.data.max_hshift_factor = 0
            if 'encourage_non_overlap_single_channel' not in config.data:
                config.data.encourage_non_overlap_single_channel = False

        if 'skip_bottom_layers_count' in config.model:
            config.model.skip_bottom_layers_count = 0

        if 'logvar_lowerbound' not in config.model:
            config.model.logvar_lowerbound = None
        if 'train_aug_rotate' not in config.data:
            config.data.train_aug_rotate = False
        if 'multiscale_lowres_separate_branch' not in config.model:
            config.model.multiscale_lowres_separate_branch = False
        if 'multiscale_retain_spatial_dims' not in config.model:
            config.model.multiscale_retain_spatial_dims = False
        config.data.train_aug_rotate = False

        if 'randomized_channels' not in config.data:
            config.data.randomized_channels = False

        if 'predict_logvar' not in config.model:
            config.model.predict_logvar = None
        if config.data.data_type in [
                DataType.OptiMEM100_014, DataType.CustomSinosoid, DataType.CustomSinosoidThreeCurve,
                DataType.SeparateTiffData
        ]:
            if custom_image_size is not None:
                old_image_size = config.data.image_size
                config.data.image_size = custom_image_size
            if use_deterministic_grid is not None:
                config.data.deterministic_grid = use_deterministic_grid
            if threshold is not None:
                config.data.threshold = threshold
            if val_repeat_factor is not None:
                config.training.val_repeat_factor = val_repeat_factor
            config.model.mode_pred = not compute_kl_loss

    print(config)
    with config.unlocked():
        config.model.skip_nboundary_pixels_from_loss = None

    ## Disentanglement setup.
    ####
    ####
    grid_alignment = GridAlignement.Center
    if image_size_for_grid_centers is not None:
        old_grid_size = config.data.get('grid_size', "grid_size not present")
        with config.unlocked():
            config.data.grid_size = image_size_for_grid_centers
            config.data.val_grid_size = image_size_for_grid_centers

    padding_kwargs = {
        'mode': config.data.get('padding_mode', 'constant'),
    }

    if padding_kwargs['mode'] == 'constant':
        padding_kwargs['constant_values'] = config.data.get('padding_value', 0)

    dloader_kwargs = {'overlapping_padding_kwargs': padding_kwargs, 'grid_alignment': grid_alignment}

    if 'multiscale_lowres_count' in config.data and config.data.multiscale_lowres_count is not None:
        data_class = LCMultiChDloader
        dloader_kwargs['num_scales'] = config.data.multiscale_lowres_count
        dloader_kwargs['padding_kwargs'] = padding_kwargs
    elif config.data.data_type == DataType.SemiSupBloodVesselsEMBL:
        data_class = SingleChannelDloader
    else:
        data_class = MultiChDloader
    if config.data.data_type in [
            DataType.CustomSinosoid, DataType.CustomSinosoidThreeCurve, DataType.AllenCellMito,
            DataType.SeparateTiffData, DataType.SemiSupBloodVesselsEMBL, DataType.BioSR_MRC
    ]:
        datapath = data_dir
    elif config.data.data_type == DataType.OptiMEM100_014:
        datapath = os.path.join(data_dir, 'OptiMEM100x014.tif')
    elif config.data.data_type == DataType.Prevedel_EMBL:
        datapath = os.path.join(data_dir, 'MS14__z0_8_sl4_fr10_p_10.1_lz510_z13_bin5_00001.tif')

    normalized_input = config.data.normalized_input
    use_one_mu_std = config.data.use_one_mu_std
    train_aug_rotate = config.data.train_aug_rotate
    enable_random_cropping = config.data.deterministic_grid is False

    train_dset = data_class(config.data,
                            datapath,
                            datasplit_type=DataSplitType.Train,
                            val_fraction=config.training.val_fraction,
                            test_fraction=config.training.test_fraction,
                            normalized_input=normalized_input,
                            use_one_mu_std=use_one_mu_std,
                            enable_rotation_aug=train_aug_rotate,
                            enable_random_cropping=enable_random_cropping,
                            **dloader_kwargs)
    import gc
    gc.collect()
    max_val = train_dset.get_max_val()
    val_dset = data_class(
        config.data,
        datapath,
        datasplit_type=eval_datasplit_type,
        val_fraction=config.training.val_fraction,
        test_fraction=config.training.test_fraction,
        normalized_input=normalized_input,
        use_one_mu_std=use_one_mu_std,
        enable_rotation_aug=False,  # No rotation aug on validation
        enable_random_cropping=False,
        # No random cropping on validation. Validation is evaluated on determistic grids
        max_val=max_val,
        **dloader_kwargs)

    # For normalizing, we should be using the training data's mean and std.
    mean_val, std_val = train_dset.compute_mean_std()
    train_dset.set_mean_std(mean_val, std_val)
    val_dset.set_mean_std(mean_val, std_val)

    if evaluate_train:
        val_dset = train_dset

    with config.unlocked():
        if config.data.data_type in [
                DataType.OptiMEM100_014, DataType.CustomSinosoid, DataType.CustomSinosoidThreeCurve,
                DataType.SeparateTiffData
        ] and old_image_size is not None:
            config.data.image_size = old_image_size

    mean_dict = {'input': None, 'target': None}
    std_dict = {'input': None, 'target': None}
    inp_fr_mean, inp_fr_std = train_dset.get_mean_std()
    mean_sq = inp_fr_mean.squeeze()
    std_sq = inp_fr_std.squeeze()
    assert mean_sq[0] == mean_sq[1] and len(mean_sq) == config.data.get('num_channels', 2)
    assert std_sq[0] == std_sq[1] and len(std_sq) == config.data.get('num_channels', 2)
    mean_dict['input'] = np.mean(inp_fr_mean, axis=1, keepdims=True)
    std_dict['input'] = np.mean(inp_fr_std, axis=1, keepdims=True)

    if config.data.target_separate_normalization is True:
        target_data_mean, target_data_std = train_dset.compute_individual_mean_std()
    else:
        target_data_mean, target_data_std = train_dset.get_mean_std()

    mean_dict['target'] = target_data_mean
    std_dict['target'] = target_data_std
    ######

    model = create_model(config, mean_dict, std_dict)

    ckpt_fpath = get_best_checkpoint(ckpt_dir)
    checkpoint = torch.load(ckpt_fpath)

    _ = model.load_state_dict(checkpoint['state_dict'], strict=False)
    model.eval()
    _ = model.cuda()

    # model.data_mean = model.data_mean.cuda()
    # model.data_std = model.data_std.cuda()
    model.set_params_to_same_device_as(torch.Tensor([1]).cuda())
    print('Loading from epoch', checkpoint['epoch'])

    def count_parameters(model):
        return sum(p.numel() for p in model.parameters() if p.requires_grad)

    print(f'Model has {count_parameters(model)/1000_000:.3f}M parameters')

    if config.data.multiscale_lowres_count is not None and custom_image_size is not None:
        model.reset_for_different_output_size(custom_image_size)

    pred_tiled, rec_loss, *_ = get_dset_predictions(
        model,
        val_dset,
        batch_size,
        num_workers=num_workers,
        mmse_count=mmse_count,
        model_type=config.model.model_type,
    )
    if pred_tiled.shape[-1] != val_dset.get_img_sz():
        pad = (val_dset.get_img_sz() - pred_tiled.shape[-1]) // 2
        pred_tiled = np.pad(pred_tiled, ((0, 0), (0, 0), (pad, pad), (pad, pad)))

    pred = stitch_predictions(pred_tiled, val_dset)
    if pred.shape[-1] == 2 and pred[..., 1].std() == 0:
        print('Denoiser model. Ignoring the second channel')
        pred = pred[..., :1].copy()

    print('Stitched predictions shape before ignoring boundary pixels', pred.shape)

    def print_ignored_pixels():
        ignored_pixels = 1
        while (pred[
                :10,
                -ignored_pixels:,
                -ignored_pixels:,
        ].std() == 0):
            ignored_pixels += 1
        ignored_pixels -= 1
        # print(f'In {pred.shape}, {ignored_pixels} many rows and columns are all zero.')
        return ignored_pixels

    actual_ignored_pixels = print_ignored_pixels()
    assert ignored_last_pixels == actual_ignored_pixels
    tar = val_dset._data

    def ignore_pixels(arr):
        if ignore_first_pixels:
            arr = arr[:, ignore_first_pixels:, ignore_first_pixels:]
        if ignored_last_pixels:
            arr = arr[:, :-ignored_last_pixels, :-ignored_last_pixels]
        return arr

    pred = ignore_pixels(pred)
    tar = ignore_pixels(tar)
    print('Stitched predictions shape after', pred.shape)

    sep_mean, sep_std = model.data_mean['target'], model.data_std['target']
    sep_mean = sep_mean.squeeze().reshape(1, 1, 1, -1)
    sep_std = sep_std.squeeze().reshape(1, 1, 1, -1)

    # tar1, tar2 = val_dset.normalize_img(tar[...,0], tar[...,1])
    tar_normalized = (tar - sep_mean.cpu().numpy()) / sep_std.cpu().numpy()
    pred_unnorm = pred * sep_std.cpu().numpy() + sep_mean.cpu().numpy()
    ch1_pred_unnorm = pred_unnorm[..., 0]
    # pred is already normalized. no need to do it.
    pred1 = pred[..., 0].astype(np.float32)
    tar1 = tar_normalized[..., 0]
    rmse1 = np.sqrt(((pred1 - tar1)**2).reshape(len(pred1), -1).mean(axis=1))
    rmse = rmse1
    rmse2 = np.array([0])

    if not normalized_ssim:
        ssim1_mean, ssim1_std = avg_ssim(tar[..., 0], ch1_pred_unnorm)
    else:
        ssim1_mean, ssim1_std = avg_ssim(tar_normalized[..., 0], pred[..., 0])

    pred2 = None
    if pred.shape[-1] == 2:
        ch2_pred_unnorm = pred_unnorm[..., 1]
        # pred is already normalized. no need to do it.
        pred2 = pred[..., 1].astype(np.float32)
        tar2 = tar_normalized[..., 1]
        rmse2 = np.sqrt(((pred2 - tar2)**2).reshape(len(pred2), -1).mean(axis=1))
        rmse = (rmse1 + rmse2) / 2

        if not normalized_ssim:
            ssim2_mean, ssim2_std = avg_ssim(tar[..., 1], ch2_pred_unnorm)
        else:
            ssim2_mean, ssim2_std = avg_ssim(tar_normalized[..., 1], pred[..., 1])
    rmse = np.round(rmse, 3)

    # Computing the output statistics.
    output_stats = {}
    output_stats['rec_loss'] = rec_loss.mean()
    output_stats['rmse'] = [np.mean(rmse1), np.array(0.0), np.array(0.0)]  #, np.mean(rmse2), np.mean(rmse)]
    output_stats['psnr'] = [avg_psnr(tar1, pred1), np.array(0.0)]  #, avg_psnr(tar2, pred2)]
    output_stats['rangeinvpsnr'] = [avg_range_inv_psnr(tar1, pred1), np.array(0.0)]  #, avg_range_inv_psnr(tar2, pred2)]
    output_stats['ssim'] = [ssim1_mean, np.array(0.0), ssim1_std, np.array(0.0)]

    if pred.shape[-1] == 2:
        output_stats['rmse'][1] = np.mean(rmse2)
        output_stats['psnr'][1] = avg_psnr(tar2, pred2)
        output_stats['rangeinvpsnr'][1] = avg_range_inv_psnr(tar2, pred2)
        output_stats['ssim'] = [ssim1_mean, ssim2_mean, ssim1_std, ssim2_std]

    output_stats['normalized_ssim'] = normalized_ssim

    print(print_token)
    print('Rec Loss', np.round(output_stats['rec_loss'], 3))
    print('RMSE', output_stats['rmse'][0].round(3), output_stats['rmse'][1].round(3), output_stats['rmse'][2].round(3))
    print('PSNR', output_stats['psnr'][0], output_stats['psnr'][1])
    print('RangeInvPSNR', output_stats['rangeinvpsnr'][0], output_stats['rangeinvpsnr'][1])
    ssim_str = 'SSIM normalized:' if normalized_ssim else 'SSIM:'
    print(ssim_str, output_stats['ssim'][0].round(3), output_stats['ssim'][1].round(3), '±',
          np.mean(output_stats['ssim'][2:4]).round(4))
    print()
    # highres data
    if config.data.data_type == DataType.SeparateTiffData:
        highres_data = get_highres_data_ventura(data_dir, config, eval_datasplit_type)
        highres_data = ignore_pixels(highres_data)
        _ = compute_high_snr_stats(config, highres_data, [ch1_pred_unnorm, ch2_pred_unnorm])
        print('')
    return output_stats, pred_unnorm


def save_hardcoded_ckpt_evaluations_to_file(normalized_ssim=True, save_prediction=False, mmse_count=1):
    ckpt_dirs = [
        '/home/ashesh.ashesh/training/disentangle/2402/D16-M23-S0-L0/36',
        '/home/ashesh.ashesh/training/disentangle/2402/D16-M23-S0-L0/35',
        '/home/ashesh.ashesh/training/disentangle/2402/D16-M23-S0-L0/32',
        '/home/ashesh.ashesh/training/disentangle/2402/D16-M23-S0-L0/30',
        '/home/ashesh.ashesh/training/disentangle/2402/D16-M23-S0-L0/31',
        '/home/ashesh.ashesh/training/disentangle/2402/D16-M23-S0-L0/33',
        '/home/ashesh.ashesh/training/disentangle/2402/D16-M23-S0-L0/43',
        '/home/ashesh.ashesh/training/disentangle/2402/D16-M23-S0-L0/49',
        '/home/ashesh.ashesh/training/disentangle/2402/D16-M23-S0-L0/55',
    ]
    if ckpt_dirs[0].startswith('/home/ashesh.ashesh'):
        OUTPUT_DIR = os.path.expanduser('/group/jug/ashesh/data/paper_stats/')
    elif ckpt_dirs[0].startswith('/home/ubuntu/ashesh'):
        OUTPUT_DIR = os.path.expanduser('~/data/paper_stats/')
    else:
        raise Exception('Invalid server')

    ckpt_dirs = [x[:-1] if '/' == x[-1] else x for x in ckpt_dirs]

    patchsz_gridsz_tuples = [(None, 64)]
    for custom_image_size, image_size_for_grid_centers in patchsz_gridsz_tuples:
        for eval_datasplit_type in [DataSplitType.All]:
            for ckpt_dir in ckpt_dirs:
                data_type = int(os.path.basename(os.path.dirname(ckpt_dir)).split('-')[0][1:])
                if data_type in [
                        DataType.OptiMEM100_014, DataType.SemiSupBloodVesselsEMBL, DataType.Pavia2VanillaSplitting,
                        DataType.ExpansionMicroscopyMitoTub, DataType.ShroffMitoEr, DataType.HTIba1Ki67
                ]:
                    ignored_last_pixels = 32
                elif data_type == DataType.BioSR_MRC:
                    ignored_last_pixels = 44
                else:
                    ignored_last_pixels = 0

                if custom_image_size is None:
                    custom_image_size = load_config(ckpt_dir).data.image_size

                handler = PaperResultsHandler(OUTPUT_DIR, eval_datasplit_type, custom_image_size,
                                              image_size_for_grid_centers, mmse_count, ignored_last_pixels)
                data, prediction = main(
                    ckpt_dir,
                    DEBUG,
                    image_size_for_grid_centers=image_size_for_grid_centers,
                    mmse_count=mmse_count,
                    custom_image_size=custom_image_size,
                    batch_size=8,
                    num_workers=4,
                    COMPUTE_LOSS=False,
                    use_deterministic_grid=None,
                    threshold=None,  # 0.02,
                    compute_kl_loss=False,
                    evaluate_train=False,
                    eval_datasplit_type=eval_datasplit_type,
                    val_repeat_factor=None,
                    psnr_type='range_invariant',
                    ignored_last_pixels=ignored_last_pixels,
                    ignore_first_pixels=0,
                    print_token=handler.dirpath(),
                    normalized_ssim=normalized_ssim,
                )
                fpath = handler.save(ckpt_dir, data)
                # except:
                #     print('FAILED for ', handler.get_output_fpath(ckpt_dir))
                #     continue
                print(handler.load(fpath))
                print('')
                print('')
                print('')
                if save_prediction:
                    offset = prediction.min()
                    prediction -= offset
                    prediction = prediction.astype(np.uint16)
                    handler.dump_predictions(ckpt_dir, prediction, {'offset': str(offset)})


if __name__ == '__main__':
    DEBUG = False
    parser = argparse.ArgumentParser()
    parser.add_argument('--ckpt_dir', type=str)
    parser.add_argument('--patch_size', type=int, default=64)
    parser.add_argument('--grid_size', type=int, default=16)
    parser.add_argument('--hardcoded', action='store_true')
    parser.add_argument('--normalized_ssim', action='store_true')
    parser.add_argument('--save_prediction', action='store_true')
    parser.add_argument('--mmse_count', type=int, default=1)

    args = parser.parse_args()
    if args.hardcoded:
        print('Ignoring ckpt_dir,patch_size and grid_size')
        save_hardcoded_ckpt_evaluations_to_file(normalized_ssim=args.normalized_ssim,
                                                save_prediction=args.save_prediction,
                                                mmse_count=args.mmse_count)
    else:
        mmse_count = 1
        ignored_last_pixels = 32 if os.path.basename(os.path.dirname(args.ckpt_dir)).split('-')[0][1:] == '3' else 0
        OUTPUT_DIR = ''
        eval_datasplit_type = DataSplitType.Test

        data = main(
            args.ckpt_dir,
            DEBUG,
            image_size_for_grid_centers=args.grid_size,
            mmse_count=mmse_count,
            custom_image_size=args.patch_size,
            batch_size=16,
            num_workers=4,
            COMPUTE_LOSS=False,
            use_deterministic_grid=None,
            threshold=None,  # 0.02,
            compute_kl_loss=False,
            evaluate_train=False,
            eval_datasplit_type=eval_datasplit_type,
            val_repeat_factor=None,
            psnr_type='range_invariant',
            ignored_last_pixels=ignored_last_pixels,
            ignore_first_pixels=0,
            normalized_ssim=args.normalized_ssim,
        )

        print('')
        print('Paper Related Stats')
        print('PSNR', np.mean(data['rangeinvpsnr']))
        print('SSIM', np.mean(data['ssim'][:2]))
