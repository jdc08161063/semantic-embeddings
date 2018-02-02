import sys
sys.path.append('models/DenseNet')

import numpy as np

import keras
from keras import backend as K

from models import cifar_resnet, vgg16, wide_residual_network as wrn
import densenet
from clr_callback import CyclicLR
from sgdr_callback import SGDR



ARCHITECTURES = ['simple', 'simple-mp', 'resnet-32', 'resnet-110', 'resnet-110-fc', 'wrn-28-10', 'densenet-100-12', 'vgg16', 'resnet-50']

LR_SCHEDULES = ['SGD', 'SGDR', 'CLR', 'ResNet-Schedule']



def squared_distance(y_true, y_pred):
    return K.sum(K.square(y_pred - y_true), axis=-1)


def mean_distance(y_true, y_pred):
    return K.sqrt(K.sum(K.square(y_pred - y_true), axis=-1))


def nn_accuracy(embedding):

    def nn_accuracy(y_true, y_pred):

        centroids = K.constant(embedding.T)
        centroids_norm = K.constant((embedding.T ** 2).sum(axis = 0, keepdims = True))
        pred_norm = K.sum(K.square(y_pred), axis = 1, keepdims = True)
        dist = pred_norm + centroids_norm - 2 * K.dot(y_pred, centroids)

        true_dist = K.sum(K.square(y_pred - y_true), axis = -1)

        return K.cast(K.less(K.abs(true_dist - K.min(dist, axis = -1)), 1e-6), K.floatx())
    
    return nn_accuracy


def build_simplenet(output_dim, filters, activation = 'relu', regularizer = keras.regularizers.l2(0.0005), final_activation = None, name = None):
    
    prefix = '' if name is None else name + '_'
    
    flattened = False
    layers = [
        keras.layers.Conv2D(filters[0], (3, 3), padding = 'same', activation = activation, kernel_regularizer = regularizer, input_shape = (32, 32, 3), name = prefix + 'conv1'),
        keras.layers.BatchNormalization(name = prefix + 'bn1')
    ]
    for i, f in enumerate(filters[1:], start = 2):
        if f == 'mp':
            layers.append(keras.layers.MaxPooling2D(pool_size = (2,2), name = '{}mp{}'.format(prefix, i)))
        elif f == 'ap':
            layers.append(keras.layers.AveragePooling2D(pool_size = (2,2), name = '{}ap{}'.format(prefix, i)))
        elif f == 'gap':
            layers.append(keras.layers.GlobalAvgPool2D(name = prefix + 'avg_pool'))
            flattened = True
        elif isinstance(f, str) and f.startswith('fc'):
            if not flattened:
                layers.append(keras.layers.Flatten(name = prefix + 'flatten'))
                flattened = True
            layers.append(keras.layers.Dense(int(f[2:]), activation = activation, kernel_regularizer = regularizer, name = '{}fc{}'.format(prefix, i)))
            layers.append(keras.layers.BatchNormalization(name = '{}bn{}'.format(prefix, i)))
        else:
            layers.append(keras.layers.Conv2D(f, (3, 3), padding = 'same', activation = activation, kernel_regularizer = regularizer, name = '{}conv{}'.format(prefix, i)))
            layers.append(keras.layers.BatchNormalization(name = '{}bn{}'.format(prefix, i)))
    
    if not flattened:
        layers.append(keras.layers.Flatten(name = prefix + 'flatten'))
        flattened = True
    layers.append(keras.layers.Dense(output_dim, activation = final_activation, name = prefix + ('prob' if final_activation == 'softmax' else 'embedding')))
    
    return keras.models.Sequential(layers, name = name)


def build_network(num_outputs, architecture, classification = False, name = None):
    
    # CIFAR-100 architectures
    
    if architecture == 'resnet-32':
        
        return cifar_resnet.SmallResNet(5, filters = [16, 32, 64] if classification else [32, 64, num_outputs],
                                        include_top = classification, classes = num_outputs, name = name)
        
    elif architecture == 'resnet-110':
        
        return cifar_resnet.SmallResNet(18, filters = [16, 32, 64] if classification else [32, 64, num_outputs],
                                        include_top = classification, classes = num_outputs, name = name)
    
    elif architecture == 'resnet-110-fc':
        
        return cifar_resnet.SmallResNet(18, filters = [32, 64, 128],
                                        include_top = True, top_activation = 'softmax' if classification else None,
                                        classes = num_outputs, name = name)
    
    elif architecture == 'wrn-28-10':
        
        return wrn.create_wide_residual_network((32, 32, 3), nb_classes = num_outputs, N = 4, k = 10, verbose = 0,
                                                final_activation = 'softmax' if classification else None)
        
    elif architecture == 'densenet-100-12':
        
        return densenet.DenseNet(growth_rate = 12, depth = 100, bottleneck = False,
                                 classes = num_outputs, activation = 'softmax' if classification else None, name = name)
        
    elif architecture == 'simple':
        
        return build_simplenet(num_outputs, [64, 64, 'ap', 128, 128, 128, 'ap', 256, 256, 256, 'ap', 512, 'gap', 'fc512'],
                               final_activation = 'softmax' if classification else None,
                               name = name)
    
    elif architecture == 'simple-mp':
        
        return build_simplenet(num_outputs, [64, 64, 'mp', 128, 128, 128, 'mp', 256, 256, 256, 'mp', 512, 'gap', 'fc512'],
                               final_activation = 'softmax' if classification else None,
                               name = name)
    
    # ImageNet architectures
    
    elif architecture == 'vgg16':
        
        return vgg16.VGG16(classes = num_outputs, final_activation = 'softmax' if classification else None)
    
    elif architecture == 'resnet-50':
        
        rn50 = keras.applications.ResNet50(include_top=False, weights=None)
        x = keras.layers.GlobalAvgPool2D(name='avg_pool')(rn50.layers[-2].output)
        x = keras.layers.Dense(num_outputs, activation = 'softmax' if classification else None, name = 'prob' if classification else 'embedding')(x)
        return keras.models.Model(rn50.inputs, x, name=name)
    
    else:
        
        raise ValueError('Unknown network architecture: {}'.format(architecture))


def get_custom_objects(architecture):
    
    if architecture in ('resnet-32', 'resnet-110', 'resnet-110-fc'):
        return { 'ChannelPadding' : cifar_resnet.ChannelPadding }
    else:
        return {}


def get_lr_schedule(schedule, num_samples, batch_size, schedule_args = {}):

    if schedule.lower() == 'sgd':
    
        if 'sgd_patience' not in schedule_args:
            schedule_args['sgd_patience'] = 10
        if 'sgd_min_lr' not in schedule_args:
            schedule_args['sgd_min_lr'] = 1e-4
        return [
            keras.callbacks.ReduceLROnPlateau('val_loss', patience = schedule_args['sgd_patience'], epsilon = 1e-4, min_lr = schedule_args['sgd_min_lr'], verbose = True)
        ], 200
    
    elif schedule.lower() == 'sgdr':
    
        if 'sgdr_base_len' not in schedule_args:
            schedule_args['sgdr_base_len'] = 12
        if 'sgdr_mul' not in schedule_args:
            schedule_args['sgdr_mul'] = 2
        if 'sgdr_max_lr' not in schedule_args:
            schedule_args['sgdr_max_lr'] = 0.1
        return (
            [SGDR(1e-6, schedule_args['sgdr_max_lr'], schedule_args['sgdr_base_len'], schedule_args['sgdr_mul'])],
            sum(schedule_args['sgdr_base_len'] * (schedule_args['sgdr_mul'] ** i) for i in range(5))
        )
        
    elif schedule.lower() == 'clr':
    
        if 'clr_step_len' not in schedule_args:
            schedule_args['clr_step_len'] = 12
        if 'clr_min_lr' not in schedule_args:
            schedule_args['clr_min_lr'] = 1e-5
        if 'clr_max_lr' not in schedule_args:
            schedule_args['clr_max_lr'] = 0.1
        return (
            [CyclicLR(schedule_args['clr_min_lr'], schedule_args['clr_max_lr'], schedule_args['clr_step_len'] * (num_samples // batch_size), mode = 'triangular')],
            schedule_args['clr_step_len'] * 20
        )
    
    elif schedule.lower() == 'resnet-schedule':
    
        def resnet_scheduler(epoch):
            if epoch >= 120:
                return 0.001
            elif epoch >= 80:
                return 0.01
            elif epoch >= 1:
                return 0.1
            else:
                return 0.01
        
        return [keras.callbacks.LearningRateScheduler(resnet_scheduler)], 164
    
    else:
    
        raise ValueError('Unknown learning rate schedule: {}'.format(schedule))
