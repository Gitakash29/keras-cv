# Copyright 2022 The KerasCV Authors
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     https://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import tensorflow as tf
from tensorflow import keras
from tensorflow.keras import layers

from keras_cv.layers.spatial_pyramid import SpatialPyramidPooling
from keras_cv.models import utils
from keras_cv.models.weights import parse_weights


@keras.utils.register_keras_serializable(package="keras_cv")
class DeepLabV3(keras.Model):
    # TODO: add a code example in the docstring.
    """A segmentation model based on DeepLab v3.

    Args:
        num_classes: int, the number of classes for the detection model. Note
            that the num_classes doesn't contain the background class, and the
            classes from the data should be represented by integers with range
            [0, num_classes).
        backbone: Optional backbone network for the model. Should be a KerasCV
            model.
        weights: Weights for the complete DeepLabV3 model. one of `None` (random
            initialization), a pretrained weight file path, or a reference to
            pre-trained weights (e.g. 'imagenet/classification' or
            'voc/segmentation') (see available pre-trained weights in
            weights.py)
        spatial_pyramid_pooling: Also known as Atrous Spatial Pyramid Pooling
            (ASPP). Performs spatial pooling on different spatial levels in the
            pyramid, with dilation.
        segmentation_head: Optional `keras.Layer` that predict the segmentation
            mask based on feature from backbone and feature from decoder.
    """

    def __init__(
        self,
        num_classes,
        backbone,
        spatial_pyramid_pooling=None,
        segmentation_head=None,
        segmentation_head_activation="softmax",
        weight_decay=0.0001,
        input_shape=(None, None, 3),
        input_tensor=None,
        weights=None,
        **kwargs,
    ):
        if not isinstance(backbone, keras.layers.Layer):
            raise ValueError(
                "Argument `backbone` must be a `keras.layers.Layer` instance. "
                f"Received instead backbone={backbone} (of type "
                f"{type(backbone)})."
            )

        if weights and not tf.io.gfile.exists(
            parse_weights(weights, True, "deeplabv3")
        ):
            raise ValueError(
                "The `weights` argument should be either `None` or the path "
                "to the weights file to be loaded. Weights file not found at "
                "location: {weights}"
            )

        inputs = utils.parse_model_inputs(input_shape, input_tensor)

        if input_shape[0] is None and input_shape[1] is None:
            input_shape = backbone.input_shape[1:]
            inputs = layers.Input(tensor=input_tensor, shape=input_shape)

        if input_shape[0] is None and input_shape[1] is None:
            raise ValueError(
                "Input shapes for both the backbone and DeepLabV3 cannot be "
                "`None`. Received: input_shape={input_shape} and "
                "backbone.input_shape={backbone.input_shape[1:]}"
            )

        height = input_shape[0]
        width = input_shape[1]

        feature_map = backbone(inputs)
        if spatial_pyramid_pooling is None:
            spatial_pyramid_pooling = SpatialPyramidPooling(
                dilation_rates=[6, 12, 18]
            )

        output = spatial_pyramid_pooling(feature_map)
        output = keras.layers.UpSampling2D(
            size=(
                height // feature_map.shape[1],
                width // feature_map.shape[2],
            ),
            interpolation="bilinear",
        )(output)

        if segmentation_head is None:
            segmentation_head = SegmentationHead(
                num_classes=num_classes,
                name="segmentation_head",
                convolutions=1,
                dropout=0.2,
                kernel_size=1,
                activation=segmentation_head_activation,
            )

        # Segmentation head expects a multiple-level output dictionary
        output = segmentation_head({1: output})

        super().__init__(
            inputs={
                "inputs": inputs,
            },
            outputs={
                "output": output,
            },
            **kwargs,
        )

        if weights is not None:
            self.load_weights(parse_weights(weights, True, "deeplabv3"))

        # All references to `self` below this line
        self.num_classes = num_classes
        self.backbone = backbone
        self.spatial_pyramid_pooling = spatial_pyramid_pooling
        self.segmentation_head = segmentation_head
        self.segmentation_head_activation = segmentation_head_activation
        self.weight_decay = weight_decay

    def build(self, input_shape):
        height = input_shape[1]
        width = input_shape[2]
        feature_map_shape = self.backbone.compute_output_shape(input_shape)
        self.up_layer = keras.layers.UpSampling2D(
            size=(
                height // feature_map_shape[1],
                width // feature_map_shape[2],
            ),
            interpolation="bilinear",
        )

    def train_step(self, data):
        images, y_true, sample_weight = keras.utils.unpack_x_y_sample_weight(
            data
        )
        with tf.GradientTape() as tape:
            y_pred = self(images, training=True)
            total_loss = self.compute_loss(
                images, y_true, y_pred, sample_weight
            )
            reg_losses = []
            if self.weight_decay:
                for var in self.trainable_variables:
                    if "bn" not in var.name:
                        reg_losses.append(
                            self.weight_decay * tf.nn.l2_loss(var)
                        )
                l2_loss = tf.math.add_n(reg_losses)
                total_loss += l2_loss
        self.optimizer.minimize(total_loss, self.trainable_variables, tape=tape)
        return self.compute_metrics(
            images, y_true, y_pred, sample_weight=sample_weight
        )

    def get_config(self):
        return {
            "num_classes": self.num_classes,
            "backbone": self.backbone,
            "spatial_pyramid_pooling": self.spatial_pyramid_pooling,
            "segmentation_head": self.segmentation_head,
            "segmentation_head_activation": self.segmentation_head_activation,
            "weight_decay": self.weight_decay,
        }


@keras.utils.register_keras_serializable(package="keras_cv")
class SegmentationHead(layers.Layer):
    """Prediction head for the segmentation model

    The head will take the output from decoder (eg FPN or ASPP), and produce a
    segmentation mask (pixel level classifications) as the output for the model.

    Args:
        num_classes: int, number of output classes for the prediction. This
            should include all the classes (e.g. background) for the model to
            predict.
        convolutions: int, number of `Conv2D` layers that are stacked before the
            final classification layer, defaults to 2.
        filters: int, number of filter/channels for the conv2D layers.
            Defaults to 256.
        activations: str or function, activation functions between the conv2D
            layers and the final classification layer, defaults to `"relu"`.
        output_scale_factor: int, or a pair of ints. Factor for upsampling the
            output mask. This is useful to scale the output mask back to same
            size as the input image. When single int is provided, the mask will
            be scaled with same ratio on both width and height. When a pair of
            ints are provided, they will be parsed as `(height_factor,
            width_factor)`. Defaults to `None`, which means no resize will
            happen to the output mask tensor.
        kernel_size: int, the kernel size to be used in each of the
            convolutional blocks, defaults to 3.
        use_bias: boolean, whether to use bias or not in each of the
            convolutional blocks, defaults to False since the blocks use
            `BatchNormalization` after each convolution, rendering bias
            obsolete.
        activation: str or function, activation to apply in the classification
            layer (output of the head), defaults to `"softmax"`.

    Examples:

    ```python
    # Mimic a FPN output dict
    p3 = tf.ones([2, 32, 32, 3])
    p4 = tf.ones([2, 16, 16, 3])
    p5 = tf.ones([2, 8, 8, 3])
    inputs = {3: p3, 4: p4, 5: p5}

    head = SegmentationHead(num_classes=11)

    output = head(inputs)
    # output tensor has shape [2, 32, 32, 11]. It has the same resolution as
    the p3.
    ```
    """

    def __init__(
        self,
        num_classes,
        convolutions=2,
        filters=256,
        activations="relu",
        dropout=0.0,
        kernel_size=3,
        activation="softmax",
        use_bias=False,
        **kwargs,
    ):
        super().__init__(**kwargs)
        self.num_classes = num_classes
        self.convolutions = convolutions
        self.filters = filters
        self.activations = activations
        self.dropout = dropout
        self.kernel_size = kernel_size
        self.use_bias = use_bias
        self.activation = activation

        self._conv_layers = []
        self._bn_layers = []
        for i in range(self.convolutions):
            conv_name = "segmentation_head_conv_{}".format(i)
            self._conv_layers.append(
                keras.layers.Conv2D(
                    name=conv_name,
                    filters=self.filters,
                    kernel_size=self.kernel_size,
                    padding="same",
                    use_bias=self.use_bias,
                )
            )
            norm_name = "segmentation_head_norm_{}".format(i)
            self._bn_layers.append(
                keras.layers.BatchNormalization(name=norm_name)
            )

        self._classification_layer = keras.layers.Conv2D(
            name="segmentation_output",
            filters=self.num_classes,
            kernel_size=1,
            use_bias=False,
            padding="same",
            activation=self.activation,
            # Force the dtype of the classification head to float32 to avoid the
            # NAN loss issue when used with mixed precision API.
            dtype=tf.float32,
        )

        self.dropout_layer = keras.layers.Dropout(self.dropout)

    def call(self, inputs):
        """Forward path for the segmentation head.

        For now, it accepts the output from the decoder only, which is a dict
        with int key and tensor as value (level-> processed feature output). The
        head will use the lowest level of feature output as the input for the
        head.
        """
        if not isinstance(inputs, dict):
            raise ValueError(
                f"Expect inputs to be a dict. Received instead inputs={inputs}"
            )

        lowest_level = next(iter(sorted(inputs)))
        x = inputs[lowest_level]
        for conv_layer, bn_layer in zip(self._conv_layers, self._bn_layers):
            x = conv_layer(x)
            x = bn_layer(x)
            x = keras.activations.get(self.activations)(x)
            if self.dropout:
                x = self.dropout_layer(x)
        return self._classification_layer(x)

    def get_config(self):
        config = {
            "num_classes": self.num_classes,
            "convolutions": self.convolutions,
            "filters": self.filters,
            "activations": self.activations,
            "dropout": self.dropout,
            "kernel_size": self.kernel_size,
            "use_bias": self.use_bias,
            "activation": self.activation,
        }
        base_config = super().get_config()
        return dict(list(base_config.items()) + list(config.items()))
