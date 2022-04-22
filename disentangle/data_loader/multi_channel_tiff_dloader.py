from typing import Tuple, Union

import numpy as np

from disentangle.core.tiff_reader import load_tiff
from disentangle.data_loader.tiff_dloader import TiffLoader


def train_val_data(fpath, is_train: Union[None, bool], channel_1, channel_2, val_fraction=None):
    data = load_tiff(fpath)
    return _train_val_data(data, is_train, channel_1, channel_2, val_fraction=val_fraction)


def _train_val_data(data, is_train: Union[None, bool], channel_1, channel_2, val_fraction=None):
    assert data.shape[-1] > max(channel_1, channel_2), 'Invalid channels'
    data = data[..., [channel_1, channel_2]]
    if is_train is None:
        return data.astype(np.float32)

    val_start = int((1 - val_fraction) * len(data))
    if is_train:
        return data[:val_start].astype(np.float32)
    else:
        return data[val_start:].astype(np.float32)


class MultiChTiffDloader(TiffLoader):
    def __init__(self,
                 img_sz: int,
                 fpath: str,
                 channel_1: int,
                 channel_2: int,
                 is_train: Union[None, bool] = None,
                 val_fraction=None,
                 enable_flips: bool = False,
                 repeat_factor: int = 1,
                 thresh: float = None):
        super().__init__(img_sz, enable_flips=enable_flips, thresh=thresh, repeat_factor=repeat_factor)
        self._fpath = fpath

        self._data = train_val_data(self._fpath, is_train, channel_1, channel_2, val_fraction=val_fraction)

        max_val = np.quantile(self._data, 0.995)
        self._data[self._data > max_val] = max_val

        self.N = len(self._data)

        msg = f'[{self.__class__.__name__}] Sz:{img_sz} Ch:{channel_1},{channel_2}'
        msg += f' Train:{int(is_train)} N:{self.N} Flip:{int(enable_flips)} Repeat:{repeat_factor}'
        msg += f' Thresh:{thresh}'
        print(msg)

    def _load_img(self, index: int) -> Tuple[np.ndarray, np.ndarray]:
        imgs = self._data[index]
        return imgs[None, :, :, 0], imgs[None, :, :, 1]

    def get_mean_std(self):
        return self._data.mean(), self._data.std()

    def _is_content_present(self, img1: np.ndarray, img2: np.ndarray):
        met1 = self.metric(img1)
        met2 = self.metric(img2)
        if self.in_allowed_range(met1) and self.in_allowed_range(met2):
            return True
        return False
