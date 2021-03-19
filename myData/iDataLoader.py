import copy
import logging
import time
import math

import numpy as np
import torch
import torch.utils.data as td
from sklearn.utils import shuffle
from PIL import Image
from torch.autograd import Variable
import torchvision.transforms.functional as trnF
from torch.nn import functional as F


class IncrementalLoader(td.Dataset):
    def __init__(self, data, labels, classes, step_size, mem_sz, mode, args, transform=None, loader=None, shuffle_idx=None,
                 model=None,
                 base_classes=50, approach='bic'):

        print(labels)
        sort_index = np.argsort(labels)
        self.data = data[sort_index]

        labels = np.array(labels)
        self.labels = labels[sort_index]

        self.args = args

        if shuffle_idx is not None:
            # label shuffle

            print("Label shuffled")
            labels = shuffle_idx[labels]

        sort_index = np.argsort(labels)
        self.data = data[sort_index]

        labels = np.array(labels)
        self.labels = labels[sort_index]

        self.labelsNormal = np.copy(self.labels)
        self.transform = transform
        self.loader = loader
        self.total_classes = classes

        self.labels_arr = np.unique(self.labels)  # for sampling method

        self.step_size = step_size
        self.base_classes = base_classes
        self.t = 0

        self.mem_sz = mem_sz
        self.validation_buffer_size = int(mem_sz / 10) * 1
        self.mode = mode

        self.start = 0
        self.end = base_classes

        self.start_idx = 0
        self.end_idx = np.argmax(self.labelsNormal > (self.end - 1))  # end data index

        if self.end == classes:
            self.end_idx = len(labels) - 1

        self.tr_idx = range(self.end_idx)
        self.len = len(self.tr_idx)
        self.current_len = self.len

        self.approach = approach
        self.memory_buffer = []
        self.exemplar = []
        self.validation_buffer = []
        self.start_point = []
        self.end_point = []

        for i in range(classes):
            self.start_point.append(np.argmin(self.labelsNormal < i))
            self.end_point.append(np.argmax(self.labelsNormal > (i)))
            self.memory_buffer.append([])
        self.end_point[-1] = len(labels)

        # variables for sampling strategy
        self.count_forgetting = np.zeros(len(self.data))
        self.count_forgetting_list = []

        self.prev_forgetting = np.zeros(len(self.data))
        self.new_forgetting = np.zeros(len(self.data))

        self.class_index_list = []
        self.class_forgetting_count = []
        self.cls_sb_hard_idx = []

        self.grad_list = []
        self.temp_input_grad = np.zeros(len(self.data))
        self.class_grad_list = []
        self.class_grad_checkList = []

        self.KD_prob_diff = np.zeros(len(self.data))
        self.class_kd_diff_list = []

    def task_change(self):
        self.t += 1

        self.start = self.end
        self.end += self.step_size

        print('dataset start, end: ', self.start, self.end)

        self.start_idx = np.argmin(self.labelsNormal < self.start)  # start data index
        self.end_idx = np.argmax(self.labelsNormal > (self.end - 1))  # end data index
        if self.end_idx == 0:
            self.end_idx = self.labels.shape[0]

        self.tr_idx = range(self.start_idx, self.end_idx)

        # validation set for bic
        if 'bic' in self.args.trainer and self.start < self.classes:
            val_per_class = (self.validation_buffer_size//2) // self.args.step_size
            self.tr_idx = []
            for i in range(self.args.step_size):
                end = self.end_point[self.start + i]
                start = self.start_point[self.start + i]
                self.validation_buffer += range(end-val_per_class, end)
                self.tr_idx += range(start, end-val_per_class)

            print('exemplar, validation: ', len(self.exemplar), len(self.validation_buffer))

            arr = []
            for idx in self.validation_buffer:
                arr.append(self.labelsNormal[idx])
            print(arr)

        self.len = len(self.tr_idx)
        self.current_len = self.len

        if self.approach == 'ft' or self.approach == 'icarl' or self.approach == 'bic' or self.approach == 'il2m' or self.approach == 'wa' or self.approach == 'dd' or self.approach == 'split':
            self.len += len(self.exemplar)

        self.prev_forgetting = np.zeros(len(self.data))
        self.new_forgetting = np.zeros(len(self.data))
        self.count_forgetting = np.zeros(len(self.data))

        self.temp_input_grad = np.zeros(len(self.data))
        self.grad_list = []

        self.KD_prob_diff = np.zeros(len(self.data))

        #self.mem_sz += 10000

    def update_bft_buffer(self):
        self.bft_buffer = copy.deepcopy(self.memory_buffer)

        min_len = 1e8
        for arr in self.bft_buffer[:self.end]:
            min_len = max(min_len, len(arr))

        buffer_per_class = min_len

        buffer_per_class = math.ceil(self.mem_sz / self.start)

        for i in range(self.start, self.end):
            start_idx = self.start_point[i]
            end_idx = self.end_point[i]

            idx = shuffle(np.arange(start_idx, end_idx), random_state=self.t)[:buffer_per_class]

            self.bft_buffer[i] += list(idx)

        for arr in self.bft_buffer[:self.end]:
            if len(arr) > buffer_per_class:
                arr.pop()

        self.bft_exemplar = []


        for arr in self.bft_buffer[:self.end] :
            self.bft_exemplar.extend(arr)

        print(self.bft_exemplar)

    def update_exemplar(self, memory_mode=None, seed=None):

        buffer_per_class = math.ceil(self.mem_sz / self.end)
        if buffer_per_class > 5000 :
            buffer_per_class = 5000
        # first, add new exemples

        for i in range(self.start, self.end):
            start_idx = self.start_point[i]
            self.memory_buffer[i] += range(start_idx, start_idx + buffer_per_class)

        # second, throw away the previous samples
        if buffer_per_class > 0:
            for i in range(self.start):
                if len(self.memory_buffer[i]) > buffer_per_class:
                    del self.memory_buffer[i][buffer_per_class:]

        # third, select classes from previous classes, and throw away only 1 samples per class
        # randomly select classes. **random seed = self.t or start** <-- IMPORTANT!

        length = sum([len(i) for i in self.memory_buffer])
        remain = length - self.mem_sz
        if remain > 0:
            imgs_per_class = [len(i) for i in self.memory_buffer]
            selected_classes = np.argsort(imgs_per_class)[-remain:]
            for c in selected_classes:
                self.memory_buffer[c].pop()

        self.exemplar = []
        for arr in self.memory_buffer:
            self.exemplar += arr

        # validation set for bic
        if 'bic' in self.args.trainer:
            self.bic_memory_buffer = copy.deepcopy(self.memory_buffer)
            self.validation_buffer = []
            validation_per_class = (self.validation_buffer_size//2) // self.end
            if validation_per_class > 0:
                for i in range(self.end):
                    self.validation_buffer += self.bic_memory_buffer[i][-validation_per_class:]
                    del self.bic_memory_buffer[i][-validation_per_class:]

            remain = self.validation_buffer_size//2 - validation_per_class * self.end

            if remain > 0:
                imgs_per_class = [len(i) for i in self.bic_memory_buffer]
                selected_classes = np.argsort(imgs_per_class)[-remain:]
                for c in selected_classes:
                    self.validation_buffer.append(self.bic_memory_buffer[c].pop())
            self.exemplar = []
            for arr in self.bic_memory_buffer:
                self.exemplar += arr

    def normalize(self, input):
        ma = np.max(input)
        mi = np.min(input)
        output = (input - mi) / (ma - mi)
        return output

    def __len__(self):
        if self.mode == 'train':
            return self.len
        elif self.mode == 'bias':
            return len(self.validation_buffer)
        elif self.mode == 'b-ft':
            return len(self.bft_exemplar)
        else:
            return self.end_idx

    def __getitem__(self, index):
        #         time.sleep(0.1)
        if self.mode == 'train':
            if index >= self.current_len:  # for bic, ft, icarl, il2m
                index = self.exemplar[index - self.current_len]
            else:
                index = self.tr_idx[index]
        elif self.mode == 'bias':  # for bic bias correction
            index = self.validation_buffer[index]
        elif self.mode == 'b-ft':
            index = self.bft_exemplar[index]
        img = self.data[index]

        try:
            img = Image.fromarray(img)
        except:
            img = self.loader(img)

        if self.transform is not None:
            img = self.transform(img)

        return img, self.labelsNormal[index], index


class ResultLoader(td.Dataset):
    def __init__(self, data, labels, transform=None, loader=None):

        self.data = data
        self.labels = labels
        self.labelsNormal = np.copy(self.labels)
        self.transform = transform
        self.loader = loader
        self.transformLabels()

    def transformLabels(self):
        '''Change labels to one hot coded vectors'''
        b = np.zeros((self.labels.size, self.labels.max() + 1))
        b[np.arange(self.labels.size), self.labels] = 1
        self.labels = b

    def __len__(self):
        return self.labels.shape[0]

    def __getitem__(self, index):
        #         time.sleep(0.1)
        img = self.data[index]
        try:
            img = Image.fromarray(img)
        except:
            img = self.loader(img)

        if self.transform is not None:
            img = self.transform(img)

        return img, self.labelsNormal[index], index


def make_ResultLoaders(data, labels, classes, step_size, transform=None, loader=None, shuffle_idx=None,
                       base_classes=50):
    if shuffle_idx is not None:
        labels = shuffle_idx[labels]
    sort_index = np.argsort(labels)
    data = data[sort_index]
    labels = np.array(labels)
    labels = labels[sort_index]

    start = 0
    end = base_classes

    loaders = []

    while (end <= classes):

        start_idx = np.argmin(labels < start)  # start data index
        end_idx = np.argmax(labels > (end - 1))  # end data index
        if end_idx == 0:
            end_idx = data.shape[0]

        loaders.append(
            ResultLoader(data[start_idx:end_idx], labels[start_idx:end_idx], transform=transform, loader=loader))

        start = end
        end += step_size

    return loaders


def iterator(dataset_loader, batch_size, shuffle=False, drop_last=False):
    kwargs = {'num_workers': 0, 'pin_memory': False}
    return torch.utils.data.DataLoader(dataset_loader, batch_size=batch_size, shuffle=shuffle, drop_last=drop_last,
                                       **kwargs)