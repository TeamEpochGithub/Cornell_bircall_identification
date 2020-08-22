"""
baseline_preprocess.py
File to create hdf5 datasets
Usage: run it with arguments that specify the wanted dataset
"""

import datetime
import time

import h5py
import tensorflow as tf
from tensorflow import keras
from tensorflow.keras import layers
from tensorflow.keras.applications import ResNet50
from tensorflow.keras.models import Model
from multiprocessing import Process

from tqdm import tqdm
import numpy as np
from scipy.signal import resample
import librosa
from matplotlib import pyplot as plt
import random

from Noise_Extractor import filter_sound, get_frames
import data_reading
import argparse
import sound_shuffling
import preprocessing

from birdcodes import bird_code

window_size = 440
universal_sample_rate = 22000
spectrogram_slices_per_input = universal_sample_rate * 5 // window_size # = 5 seconds


class HDF5DatasetExtendable:
    VERSION = "1.0.0"
    def __init__(self, filename, data_type=np.float32, label_type=np.int, compression=None):
        """
        Set initial parameters for auto-resizable hdf5 dataset
        :param filename: The filenamae
        :param data_type: Datatype to use for storage, such as np.float32
        :param label_type: Datatype to use for storage, such as np.float32
        :param compression: None or "gzip"
        """
        assert ".hdf5" in filename, "Filename ust be .hdf5 file"
        self.filename = filename
        self.data_type = data_type
        self.label_type = label_type
        self.compression = compression
        self.initialized = False

    def __enter__(self):
        self.file = h5py.File(self.filename, "w")
        return self

    def add_metadata(self, info):
        """
        Add metedata to the dataset attributes. This data can be displayed when starting training, and
        informs the user of what this dataset contains. Be descriptive!
        Tip: a good starting point is just pass vars(args), such that all commandline options are logged.
        :param info: A dictionary with user information such as {"augmentation":"shifted"}
        """
        self.dataset.attrs["version"] = self.VERSION
        for k, v in info.items():
            self.dataset.attrs[k] = str(v)

    def _init(self, data, labels):
        """
        Initialize the dataset objects with the first batch of data.
        Do not call this function, always call append.
        :param data: numpy array containing the data, where the first axis is the sample index
        :param labels: numpy array containing the labels, where the first axis is the sample index
        """
        self.dataset = self.file.create_dataset(
            "data", np.shape(data), self.data_type, maxshape=(None,) + np.shape(data)[1:],
            data=data, chunks=True,
            compression=self.compression
        )
        self.labelset = self.file.create_dataset(
            "labels", np.shape(labels), self.label_type, maxshape=(None,) + np.shape(labels)[1:],
            data=labels, chunks=True,
            compression=self.compression
        )
        self.initialized = True

    def append(self, data, labels):
        """
        Add data to the dataset. If not initialized, this will copy the shapes from the first call and initialze.
        :param data: numpy array containing the data, where the first axis is the sample index
        :param labels: numpy array containing the labels, where the first axis is the sample index
        """
        if not self.initialized:
            self._init(data, labels)
            return

        shape = np.array(self.dataset.shape)
        shape[0] += data.shape[0]
        self.dataset.resize(shape)
        self.dataset[-data.shape[0]:, ...] = data

        shape = np.array(self.labelset.shape)
        shape[0] += labels.shape[0]
        self.labelset.resize(shape)
        self.labelset[-labels.shape[0]:, ...] = labels

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.file.close()




# if __name__ == '__main__':
#
#try:
#gpu_devices = tf.config.experimental.list_physical_devices('GPU')
#for device in gpu_devices:
#tf.config.experimental.set_memory_growth(device, True)
#except IndexError:
# 		pass
#
#def preprocess(file_path, feature_extractor: keras.models.Model):
#"""
#Loads the audio file, generates a spectrogram, and applies feature_extractor on it
#"""
# 	spectrograms = tf_fourier(file_path, args)
#
#if spectrograms != []:
# #Duplicate the single amplitude channel to 3 channels, because ResNet50 expects 3 channels
#spectrograms = np.array(spectrograms)
#spectrograms = np.reshape(spectrograms, spectrograms.shape + (1,))
# 		spectrograms = np.repeat(spectrograms, 3, axis=3)
#
# #Apply the feature extractor
#return feature_extractor.predict(spectrograms)
#else:
# 		return []

def tf_fourier(file_path, args, display=False):
    """
    Loads the audio file, and applies the short-time fourier transform implemented on the GPU by TensorFlow
    """
    try:
        sound, sample_rate = librosa.load(file_path)
    except ZeroDivisionError as e:
        raise ZeroDivisionError("File for error above:", file_path) from e

    # Resampling
    if sample_rate != universal_sample_rate:
        sound = resample(sound, int(universal_sample_rate * (len(sound) / sample_rate)))
        pass

    # If argument for shuffle augmentation is set, shuffles data based on a metric
    if args.shuffle_aug:
        pass

    # If argument for noise addition is set, adds random white- or background noise or removes noise
    if args.noise_aug:
        if args.noise_aug == "white_noise":
            sound = sound_shuffling.add_white_noise(sound, target_snr=np.random.normal(4.5, 2.0))
        if args.noise_aug == "background_noise":
            sound = sound_shuffling.add_random_background_noise(sound, sample_rate)
        if args.no_noise == "no_noise":
            sound = preprocessing.extract_noise(sound,
                                                sample_rate,
                                                window_width=2048,
                                                step_size=512,
                                                verbose=False)

    # If argument for shifting is set, shifts amplitude, frequency or time randomly
    if args.shift_aug:
        if args.shift_aug == "amplitude_shift":
            n_steps = random.randint(0, 15)
            sound = sound_shuffling.amplitude_shift(sound, n_steps)
        if args.shift_aug == "frequency_shift":
            n_steps = random.randint(-15, 15)
            sound = sound_shuffling.frequency_shift(sound, sample_rate, n_steps)
        if args.shift_aug == "time_stretch":
            n_steps = random.randint(-15, 15)
            shifted_file = sound_shuffling.time_stretch(sound, n_steps)

    # Normalize
    sound = preprocessing.normalize(sound)

    # Generate the spectrogram
    spectrogram = tf.abs(
        tf.signal.stft(tf.reshape(sound, [1, -1]), window_size, window_size)
    )[0]

    # Split up into slices of (by default) 5 seconds
    n_fragmets = spectrogram.shape[0] // spectrogram_slices_per_input
    slices = np.zeros((n_fragmets, spectrogram_slices_per_input,  spectrogram.shape[1]))

    for i in range(n_fragmets):
        begin, end = i * spectrogram_slices_per_input, (i + 1) * spectrogram_slices_per_input
        slices[i] = spectrogram[begin:end]

    print("SHAPE", slices.shape)

    return np.array(slices)
#
# spectrogram_shape = (250, 257)
# DATASET_VERSION = "1.0.0"
#
#
# resnet: keras.models.Model = ResNet50(input_shape=(spectrogram_shape + (3,)), include_top=False)
#
if __name__ == "__main__":
    import sys
    import os

    parser = argparse.ArgumentParser()
    parser.add_argument("--feature_mode", default="spectrogram", type=str, help="Possible values: 'spectrogram' or 'resnet'")
    parser.add_argument("--dir", default="preprocessed.h5", type=str, help="Where to place the hdf5 dataset file")
    parser.add_argument("--info", type=str, help="Description to add to hdf5 file metadata")
    parser.add_argument("-b", "--bird_codes", nargs="*", default=[], type=str, help="List of birdcodes indicating which files need to be processed")
    parser.add_argument("--shift_aug", type=str, default=None, help="Possible values: 'amplitude_shift, 'frequency_shift' or 'time_stretch'")
    parser.add_argument("--noise_aug", type=str, default=None, help="Possible values: 'white_noise', 'background_noise', 'no_noise' ")
    parser.add_argument("--shuffle_aug", type=int, default=None, help="Number of files to combine")
    args = parser.parse_args()

    # Test script for HDF5DatasetExtendable

    with HDF5DatasetExtendable("test.hdf5") as dataset:

        data = np.array([range(10) for x in range(10)])
        labels = np.array([range(5) for x in range(10)])

        dataset.append(data, labels)

        data = np.array([range(10) for x in range(8)]) # less samples than previous append
        labels = np.array([range(5) for x in range(8)])

        dataset.append(data, labels)

        dataset.add_metadata(vars(args))

#     output_dir = args.dir
#     use_resnet = args.feature_mode == "resnet"
#     if use_resnet:
#         raise NotImplementedError("HDF5 not set up for this, and naming scheme is incorrect (And breaking changes to shape and such)")
#
#     print("Allg birdcodes to process:", " ".join(args.bird_codes))
#
#     # Process all files based on the birdcodes in the arguments
#     if args.bird_codes == []:
#         args.bird_codes = bird_code.keys()
#
#     i = 0
#     with h5py.File(args.dir, "w") as file:
#
#         for birdcode in tqdm(args.bird_codes):
#             print(birdcode)
#             bird_id = bird_code[birdcode]
#
#             path_to_birdsound_dir = data_reading.test_data_base_dir + "train_audio/" + birdcode + "/"
#
#             for file_name in os.listdir(path_to_birdsound_dir):
#
#                 fragments = tf_fourier(path_to_birdsound_dir + file_name, args)
#
#                 # shape (?, 250, 257) -> (?, 250, 257, 1) aka add channel
#                 fragments = fragments[:, :, :, np.newaxis]
#
#                 if len(fragments) == 0:
#                     print("Skipping short sound file: ", file_name)
#                     continue
#
#                 # match number of labels to fragments
#                 labels = np.array([[bird_id]] * len(fragments)) # one hot encoding
#                 print("Shape", fragments.shape)
#                 print("Shape label", labels.shape)
#
#                 if "spectrograms" not in file:
#                     dataset = file.create_dataset(
#                         "spectrograms", np.shape(fragments), np.float32, maxshape=(None,) + spectrogram_shape + (1,),
#                         data=fragments, chunks=True,
#                         # compression="gzip"
#                     )
#                     max_birds_per_segment = 20
#                     label_set = file.create_dataset(
#                         "labels", np.shape(labels), np.int, maxshape=(None, max_birds_per_segment), data=labels, chunks=True
#                     )
#                 else:
#                     shape = np.array(dataset.shape)
#                     shape[0] += fragments.shape[0]
#                     dataset.resize(shape)
#                     dataset[-fragments.shape[0]:, ...] = fragments
#
#                     shape = np.array(label_set.shape)
#                     shape[0] += labels.shape[0]
#                     label_set.resize(shape)
#                     label_set[-labels.shape[0]:, ...] = labels
#
#             i += 1
#             if i == 3:
#                 break
#
#         dataset.attrs["version"] = DATASET_VERSION
#         dataset.attrs["feature_mode"] = args.feature_mode
#         dataset.attrs["info"] = args.info
#         dataset.attrs["creation"] = datetime.datetime.now()
#         dataset.attrs["bird_code"] = bird_code
