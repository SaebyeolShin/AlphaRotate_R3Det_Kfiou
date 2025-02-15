# -*-coding: utf-8 -*-

from __future__ import absolute_import, division, print_function

# import tensorflow as tf
import tensorflow.compat.v1 as tf
# import tensorflow.contrib.slim as slim
import tf_slim as slim

from alpharotate.libs.models.detectors.single_stage_base_network import DetectionNetworkBase
from alpharotate.libs.models.losses.losses_kfiou import LossKF
from alpharotate.libs.utils import bbox_transform
from alpharotate.libs.utils import nms_rotate
from alpharotate.libs.utils.coordinate_convert import coordinate5_2_8_tf
from alpharotate.libs.models.samplers.retinanet.anchor_sampler_retinenet import AnchorSamplerRetinaNet
from alpharotate.libs.models.samplers.r3det.refine_anchor_sampler_r3det import RefineAnchorSamplerR3Det


class DetectionNetworkR3DetKF(DetectionNetworkBase):

    def __init__(self, cfgs, is_training):
        super(DetectionNetworkR3DetKF, self).__init__(cfgs, is_training)
        self.anchor_sampler_retinenet = AnchorSamplerRetinaNet(cfgs)
        self.refine_anchor_sampler_r3det = RefineAnchorSamplerR3Det(cfgs)
        self.losses = LossKF(self.cfgs)

    def refine_cls_net(self, inputs, scope_list, reuse_flag, level):
        rpn_conv2d_3x3 = inputs
        for i in range(self.cfgs.NUM_SUBNET_CONV):
            rpn_conv2d_3x3 = slim.conv2d(inputs=rpn_conv2d_3x3,
                                         num_outputs=self.cfgs.FPN_CHANNEL,
                                         kernel_size=[3, 3],
                                         stride=1,
                                         activation_fn=None if self.cfgs.USE_GN else tf.nn.relu,
                                         weights_initializer=self.cfgs.SUBNETS_WEIGHTS_INITIALIZER,
                                         biases_initializer=self.cfgs.SUBNETS_BIAS_INITIALIZER,
                                         trainable=self.is_training,
                                         scope='{}_{}'.format(scope_list[0], i),
                                         reuse=reuse_flag)

            if self.cfgs.USE_GN:
                rpn_conv2d_3x3 = tf.contrib.layers.group_norm(rpn_conv2d_3x3)
                rpn_conv2d_3x3 = tf.nn.relu(rpn_conv2d_3x3)

        rpn_box_scores = slim.conv2d(rpn_conv2d_3x3,
                                     num_outputs=self.cfgs.CLASS_NUM,
                                     kernel_size=[3, 3],
                                     stride=1,
                                     weights_initializer=self.cfgs.SUBNETS_WEIGHTS_INITIALIZER,
                                     biases_initializer=self.cfgs.FINAL_CONV_BIAS_INITIALIZER,
                                     scope=scope_list[2],
                                     trainable=self.is_training,
                                     activation_fn=None,
                                     reuse=reuse_flag)

        rpn_box_scores = tf.reshape(rpn_box_scores, [-1, self.cfgs.CLASS_NUM],
                                    name='refine_{}_classification_reshape'.format(level))
        rpn_box_probs = tf.sigmoid(rpn_box_scores, name='refine_{}_classification_sigmoid'.format(level))

        return rpn_box_scores, rpn_box_probs

    def refine_reg_net(self, inputs, scope_list, reuse_flag, level):
        rpn_conv2d_3x3 = inputs
        for i in range(self.cfgs.NUM_SUBNET_CONV):
            rpn_conv2d_3x3 = slim.conv2d(inputs=rpn_conv2d_3x3,
                                         num_outputs=self.cfgs.FPN_CHANNEL,
                                         kernel_size=[3, 3],
                                         weights_initializer=self.cfgs.SUBNETS_WEIGHTS_INITIALIZER,
                                         biases_initializer=self.cfgs.SUBNETS_BIAS_INITIALIZER,
                                         stride=1,
                                         activation_fn=None if self.cfgs.USE_GN else tf.nn.relu,
                                         scope='{}_{}'.format(scope_list[1], i),
                                         trainable=self.is_training,
                                         reuse=reuse_flag)

            if self.cfgs.USE_GN:
                rpn_conv2d_3x3 = tf.contrib.layers.group_norm(rpn_conv2d_3x3)
                rpn_conv2d_3x3 = tf.nn.relu(rpn_conv2d_3x3)

        rpn_delta_boxes = slim.conv2d(rpn_conv2d_3x3,
                                      num_outputs=5,
                                      kernel_size=[3, 3],
                                      stride=1,
                                      weights_initializer=self.cfgs.SUBNETS_WEIGHTS_INITIALIZER,
                                      biases_initializer=self.cfgs.SUBNETS_BIAS_INITIALIZER,
                                      scope=scope_list[3],
                                      trainable=self.is_training,
                                      activation_fn=None,
                                      reuse=reuse_flag)

        rpn_delta_boxes = tf.reshape(rpn_delta_boxes, [-1, 5],
                                     name='refine_{}_regression_reshape'.format(level))
        return rpn_delta_boxes

    def refine_net(self, feature_pyramid, name):

        refine_delta_boxes_list = []
        refine_scores_list = []
        refine_probs_list = []
        with tf.variable_scope(name):
            with slim.arg_scope([slim.conv2d], weights_regularizer=slim.l2_regularizer(self.cfgs.WEIGHT_DECAY)):
                for level in self.cfgs.LEVEL:

                    if self.cfgs.SHARE_NET:
                        reuse_flag = None if level == self.cfgs.LEVEL[0] else True
                        scope_list = ['conv2d_3x3_cls', 'conv2d_3x3_reg', 'refine_classification', 'refine_regression']
                    else:
                        reuse_flag = None
                        scope_list = ['conv2d_3x3_cls_' + level, 'conv2d_3x3_reg_' + level,
                                      'refine_classification_' + level, 'refine_regression_' + level]

                    refine_box_scores, refine_box_probs = self.refine_cls_net(feature_pyramid[level],
                                                                              scope_list, reuse_flag,
                                                                              level)
                    refine_delta_boxes = self.refine_reg_net(feature_pyramid[level], scope_list, reuse_flag, level)

                    refine_scores_list.append(refine_box_scores)
                    refine_probs_list.append(refine_box_probs)
                    refine_delta_boxes_list.append(refine_delta_boxes)

            return refine_delta_boxes_list, refine_scores_list, refine_probs_list

    def refine_feature_op(self, points, feature_map, name):

        h, w = tf.cast(tf.shape(feature_map)[1], tf.int32), tf.cast(tf.shape(feature_map)[2], tf.int32)

        xmin = tf.maximum(0.0, tf.floor(points[:, 0]))
        xmin = tf.minimum(tf.cast(w - 1, tf.float32), tf.ceil(xmin))

        ymin = tf.maximum(0.0, tf.floor(points[:, 1]))
        ymin = tf.minimum(tf.cast(h - 1, tf.float32), tf.ceil(ymin))

        xmax = tf.minimum(tf.cast(w - 1, tf.float32), tf.ceil(points[:, 0]))
        xmax = tf.maximum(0.0, tf.floor(xmax))

        ymax = tf.minimum(tf.cast(h - 1, tf.float32), tf.ceil(points[:, 1]))
        ymax = tf.maximum(0.0, tf.floor(ymax))

        left_top = tf.cast(tf.transpose(tf.stack([ymin, xmin], axis=0)), tf.int32)
        right_bottom = tf.cast(tf.transpose(tf.stack([ymax, xmax], axis=0)), tf.int32)
        left_bottom = tf.cast(tf.transpose(tf.stack([ymax, xmin], axis=0)), tf.int32)
        right_top = tf.cast(tf.transpose(tf.stack([ymin, xmax], axis=0)), tf.int32)

        # feature_1x5 = slim.conv2d(inputs=feature_map,
        #                           num_outputs=self.cfgs.FPN_CHANNEL,
        #                           kernel_size=[1, 5],
        #                           weights_initializer=self.cfgs.SUBNETS_WEIGHTS_INITIALIZER,
        #                           biases_initializer=self.cfgs.SUBNETS_BIAS_INITIALIZER,
        #                           stride=1,
        #                           activation_fn=None,
        #                           trainable=self.is_training,
        #                           scope='refine_1x5_{}'.format(name))
        #
        # feature5x1 = slim.conv2d(inputs=feature_1x5,
        #                          num_outputs=self.cfgs.FPN_CHANNEL,
        #                          kernel_size=[5, 1],
        #                          weights_initializer=self.cfgs.SUBNETS_WEIGHTS_INITIALIZER,
        #                          biases_initializer=self.cfgs.SUBNETS_BIAS_INITIALIZER,
        #                          stride=1,
        #                          activation_fn=None,
        #                          trainable=self.is_training,
        #                          scope='refine_5x1_{}'.format(name))
        #
        # feature_1x1 = slim.conv2d(inputs=feature_map,
        #                           num_outputs=self.cfgs.FPN_CHANNEL,
        #                           kernel_size=[1, 1],
        #                           weights_initializer=self.cfgs.SUBNETS_WEIGHTS_INITIALIZER,
        #                           biases_initializer=self.cfgs.SUBNETS_BIAS_INITIALIZER,
        #                           stride=1,
        #                           activation_fn=None,
        #                           trainable=self.is_training,
        #                           scope='refine_1x1_{}'.format(name))
        #
        # feature = feature5x1 + feature_1x1
        feature = feature_map

        left_top_feature = tf.gather_nd(tf.squeeze(feature), left_top)
        right_bottom_feature = tf.gather_nd(tf.squeeze(feature), right_bottom)
        left_bottom_feature = tf.gather_nd(tf.squeeze(feature), left_bottom)
        right_top_feature = tf.gather_nd(tf.squeeze(feature), right_top)

        refine_feature = right_bottom_feature * tf.tile(
            tf.reshape((tf.abs((points[:, 0] - xmin) * (points[:, 1] - ymin))), [-1, 1]),
            [1, self.cfgs.FPN_CHANNEL]) \
                         + left_top_feature * tf.tile(
            tf.reshape((tf.abs((xmax - points[:, 0]) * (ymax - points[:, 1]))), [-1, 1]),
            [1, self.cfgs.FPN_CHANNEL]) \
                         + right_top_feature * tf.tile(
            tf.reshape((tf.abs((points[:, 0] - xmin) * (ymax - points[:, 1]))), [-1, 1]),
            [1, self.cfgs.FPN_CHANNEL]) \
                         + left_bottom_feature * tf.tile(
            tf.reshape((tf.abs((xmax - points[:, 0]) * (points[:, 1] - ymin))), [-1, 1]),
            [1, self.cfgs.FPN_CHANNEL])

        refine_feature = tf.reshape(refine_feature, [1, tf.cast(h, tf.int32), tf.cast(w, tf.int32), self.cfgs.FPN_CHANNEL])

        # refine_feature = tf.reshape(refine_feature, [1, tf.cast(feature_size[1], tf.int32),
        #                                              tf.cast(feature_size[0], tf.int32), 256])

        return refine_feature + feature

    def refine_feature_five_op(self, points, feature_map, name):

        h, w = tf.cast(tf.shape(feature_map)[1], tf.int32), tf.cast(tf.shape(feature_map)[2], tf.int32)

        feature_1x5 = slim.conv2d(inputs=feature_map,
                                  num_outputs=self.cfgs.FPN_CHANNEL,
                                  kernel_size=[1, 5],
                                  weights_initializer=self.cfgs.SUBNETS_WEIGHTS_INITIALIZER,
                                  biases_initializer=self.cfgs.SUBNETS_BIAS_INITIALIZER,
                                  stride=1,
                                  activation_fn=None,
                                  trainable=self.is_training,
                                  scope='refine_1x5_{}'.format(name))

        feature5x1 = slim.conv2d(inputs=feature_1x5,
                                 num_outputs=self.cfgs.FPN_CHANNEL,
                                 kernel_size=[5, 1],
                                 weights_initializer=self.cfgs.SUBNETS_WEIGHTS_INITIALIZER,
                                 biases_initializer=self.cfgs.SUBNETS_BIAS_INITIALIZER,
                                 stride=1,
                                 activation_fn=None,
                                 trainable=self.is_training,
                                 scope='refine_5x1_{}'.format(name))

        feature_1x1 = slim.conv2d(inputs=feature_map,
                                  num_outputs=self.cfgs.FPN_CHANNEL,
                                  kernel_size=[1, 1],
                                  weights_initializer=self.cfgs.SUBNETS_WEIGHTS_INITIALIZER,
                                  biases_initializer=self.cfgs.SUBNETS_BIAS_INITIALIZER,
                                  stride=1,
                                  activation_fn=None,
                                  trainable=self.is_training,
                                  scope='refine_1x1_{}'.format(name))

        feature = feature5x1 + feature_1x1

        for i in range(5):
            xmin = tf.maximum(0.0, tf.floor(points[:, 0+2*i]))
            ymin = tf.maximum(0.0, tf.floor(points[:, 1+2*i]))
            xmax = tf.minimum(tf.cast(w - 1, tf.float32), tf.ceil(points[:, 0+2*i]))
            ymax = tf.minimum(tf.cast(h - 1, tf.float32), tf.ceil(points[:, 1+2*i]))

            left_top = tf.cast(tf.transpose(tf.stack([ymin, xmin], axis=0)), tf.int32)
            right_bottom = tf.cast(tf.transpose(tf.stack([ymax, xmax], axis=0)), tf.int32)
            left_bottom = tf.cast(tf.transpose(tf.stack([ymax, xmin], axis=0)), tf.int32)
            right_top = tf.cast(tf.transpose(tf.stack([ymin, xmax], axis=0)), tf.int32)

            left_top_feature = tf.gather_nd(tf.squeeze(feature), left_top)
            right_bottom_feature = tf.gather_nd(tf.squeeze(feature), right_bottom)
            left_bottom_feature = tf.gather_nd(tf.squeeze(feature), left_bottom)
            right_top_feature = tf.gather_nd(tf.squeeze(feature), right_top)

            refine_feature = right_bottom_feature * tf.tile(
                tf.reshape((tf.abs((points[:, 0+2*(i-1)] - xmin) * (points[:, 1+2*(i-1)] - ymin))), [-1, 1]),
                [1, self.cfgs.FPN_CHANNEL]) \
                             + left_top_feature * tf.tile(
                tf.reshape((tf.abs((xmax - points[:, 0+2*(i-1)]) * (ymax - points[:, 1+2*(i-1)]))), [-1, 1]),
                [1, self.cfgs.FPN_CHANNEL]) \
                             + right_top_feature * tf.tile(
                tf.reshape((tf.abs((points[:, 0+2*(i-1)] - xmin) * (ymax - points[:, 1+2*(i-1)]))), [-1, 1]),
                [1, self.cfgs.FPN_CHANNEL]) \
                             + left_bottom_feature * tf.tile(
                tf.reshape((tf.abs((xmax - points[:, 0+2*(i-1)]) * (points[:, 1+2*(i-1)] - ymin))), [-1, 1]),
                [1, self.cfgs.FPN_CHANNEL])

            refine_feature = tf.reshape(refine_feature, [1, tf.cast(h, tf.int32), tf.cast(w, tf.int32), self.cfgs.FPN_CHANNEL])

            feature += refine_feature

        return feature

    def refine_stage(self, input_img_batch, gtboxes_batch_r, box_pred_list, cls_prob_list, proposal_list,
                     feature_pyramid, gpu_id, pos_threshold, neg_threshold,
                     stage, proposal_filter=False):
        with tf.variable_scope('refine_feature_pyramid{}'.format(stage)):
            refine_feature_pyramid = {}
            refine_boxes_list = []

            for box_pred, cls_prob, proposal, stride, level in \
                    zip(box_pred_list, cls_prob_list, proposal_list,
                        self.cfgs.ANCHOR_STRIDE, self.cfgs.LEVEL):

                if proposal_filter:
                    box_pred = tf.reshape(box_pred, [-1, self.num_anchors_per_location, 5])
                    proposal = tf.reshape(proposal, [-1, self.num_anchors_per_location, 5 if self.method == 'R' else 4])
                    cls_prob = tf.reshape(cls_prob, [-1, self.num_anchors_per_location, self.cfgs.CLASS_NUM])

                    cls_max_prob = tf.reduce_max(cls_prob, axis=-1)
                    box_pred_argmax = tf.cast(tf.reshape(tf.argmax(cls_max_prob, axis=-1), [-1, 1]), tf.int32)
                    indices = tf.cast(tf.cumsum(tf.ones_like(box_pred_argmax), axis=0), tf.int32) - tf.constant(1, tf.int32)
                    indices = tf.concat([indices, box_pred_argmax], axis=-1)

                    box_pred = tf.reshape(tf.gather_nd(box_pred, indices), [-1, 5])
                    proposal = tf.reshape(tf.gather_nd(proposal, indices), [-1, 5 if self.method == 'R' else 4])

                    if self.cfgs.METHOD == 'H':
                        x_c = (proposal[:, 2] + proposal[:, 0]) / 2
                        y_c = (proposal[:, 3] + proposal[:, 1]) / 2
                        h = proposal[:, 2] - proposal[:, 0] + 1
                        w = proposal[:, 3] - proposal[:, 1] + 1
                        theta = -90 * tf.ones_like(x_c)
                        proposal = tf.transpose(tf.stack([x_c, y_c, w, h, theta]))
                else:
                    box_pred = tf.reshape(box_pred, [-1, 5])
                    proposal = tf.reshape(proposal, [-1, 5])

                bboxes = bbox_transform.rbbox_transform_inv(boxes=proposal, deltas=box_pred)
                refine_boxes_list.append(bboxes)

                center_point = bboxes[:, :2] / stride
                refine_feature_pyramid[level] = self.refine_feature_op(points=center_point,
                                                                       feature_map=feature_pyramid[level],
                                                                       name=level)
                # points = coordinate5_2_8_tf(bboxes) / stride
                # refine_feature_pyramid[level] = self.refine_feature_five_op(points=tf.concat([points, center_point], axis=1),
                #                                                             feature_map=feature_pyramid[level],
                #                                                             name=level)

            refine_box_pred_list, refine_cls_score_list, refine_cls_prob_list = self.refine_net(refine_feature_pyramid,
                                                                                                'refine_net{}'.format(stage))

            refine_box_pred = tf.concat(refine_box_pred_list, axis=0)
            refine_cls_score = tf.concat(refine_cls_score_list, axis=0)
            # refine_cls_prob = tf.concat(refine_cls_prob_list, axis=0)
            refine_boxes = tf.concat(refine_boxes_list, axis=0)

        if self.is_training:
            with tf.variable_scope('build_refine_loss{}'.format(stage)):
                refine_labels, refine_target_delta, refine_box_states, refine_target_boxes = tf.py_func(
                    func=self.refine_anchor_sampler_r3det.refine_anchor_target_layer,
                    inp=[gtboxes_batch_r, refine_boxes, pos_threshold, neg_threshold, gpu_id],
                    Tout=[tf.float32, tf.float32,
                          tf.float32, tf.float32])

                self.add_anchor_img_smry(input_img_batch, refine_boxes, refine_box_states, 1)

                refine_cls_loss = self.losses.focal_loss(refine_labels, refine_cls_score, refine_box_states)
                # refine_reg_loss = self.losses.kalman_filter_iou_xy(refine_target_delta, refine_box_pred,
                #                                                    refine_box_states, refine_target_boxes,
                #                                                    refine_boxes, is_refine=True)
                refine_reg_sigma = self.losses.kalman_filter_iou(refine_box_pred,
                                                                 refine_box_states,
                                                                 refine_target_boxes,
                                                                 refine_boxes, is_refine=True)

                refine_target_delta = tf.reshape(refine_target_delta, [-1, 5])
                refine_box_pred = tf.reshape(refine_box_pred, [-1, 5])
                refine_reg_xy = self.losses.smooth_l1_loss(refine_target_delta[:, :2], refine_box_pred[:, :2], refine_box_states)

                self.losses_dict['refine_cls_loss{}'.format(stage)] = refine_cls_loss * self.cfgs.CLS_WEIGHT
                self.losses_dict['refine_reg_sigma{}'.format(stage)] = refine_reg_sigma * self.cfgs.REG_WEIGHT
                self.losses_dict['refine_reg_xy{}'.format(stage)] = refine_reg_xy * self.cfgs.REG_WEIGHT
                # self.losses_dict['refine_reg_loss{}'.format(stage)] = refine_reg_loss * self.cfgs.REG_WEIGHT * 50

        return refine_box_pred_list, refine_cls_prob_list, refine_boxes_list

    def build_whole_detection_network(self, input_img_batch, gtboxes_batch_h=None, gtboxes_batch_r=None, gpu_id=0):

        if self.is_training:
            gtboxes_batch_h = tf.reshape(gtboxes_batch_h, [-1, 5])
            gtboxes_batch_h = tf.cast(gtboxes_batch_h, tf.float32)

            gtboxes_batch_r = tf.reshape(gtboxes_batch_r, [-1, 6])
            gtboxes_batch_r = tf.cast(gtboxes_batch_r, tf.float32)

        if self.cfgs.USE_GN:
            input_img_batch = tf.reshape(input_img_batch, [1, self.cfgs.IMG_SHORT_SIDE_LEN,
                                                           self.cfgs.IMG_MAX_LENGTH, 3])

        # 1. build backbone
        feature_pyramid = self.build_backbone(input_img_batch)

        # 2. build rpn
        rpn_box_pred_list, rpn_cls_score_list, rpn_cls_prob_list = self.rpn_net(feature_pyramid, 'rpn_net')
        rpn_box_pred = tf.concat(rpn_box_pred_list, axis=0)
        rpn_cls_score = tf.concat(rpn_cls_score_list, axis=0)
        # rpn_cls_prob = tf.concat(rpn_cls_prob_list, axis=0)

        # 3. generate anchors
        anchor_list = self.make_anchors(feature_pyramid)
        anchors = tf.concat(anchor_list, axis=0)

        # 4. build loss
        if self.is_training:
            with tf.variable_scope('build_loss'):
                labels, target_delta, anchor_states, target_boxes = tf.py_func(func=self.anchor_sampler_retinenet.anchor_target_layer,
                                                                               inp=[gtboxes_batch_h,
                                                                                    gtboxes_batch_r, anchors, gpu_id],
                                                                               Tout=[tf.float32, tf.float32, tf.float32,
                                                                                     tf.float32])

                if self.method == 'H':
                    self.add_anchor_img_smry(input_img_batch, anchors, anchor_states, 0)
                else:
                    self.add_anchor_img_smry(input_img_batch, anchors, anchor_states, 1)

                cls_loss = self.losses.focal_loss(labels, rpn_cls_score, anchor_states)

                reg_sigma = self.losses.kalman_filter_iou(rpn_box_pred, anchor_states,
                                                          target_boxes, anchors, is_refine=False)
                target_delta = tf.reshape(target_delta, [-1, 5])
                rpn_box_pred = tf.reshape(rpn_box_pred, [-1, 5])
                reg_xy = self.losses.smooth_l1_loss(target_delta[:, :2], rpn_box_pred[:, :2], anchor_states)
                reg_loss = self.losses.smooth_l1_loss(target_delta, rpn_box_pred, anchor_states)
                # reg_loss = self.losses.kalman_filter_iou_xy(target_delta, rpn_box_pred, anchor_states,
                #                                             target_boxes, anchors, is_refine=False)

                self.losses_dict['cls_loss'] = cls_loss * self.cfgs.CLS_WEIGHT
                self.losses_dict['reg_sigma'] = reg_sigma * self.cfgs.REG_WEIGHT
                self.losses_dict['reg_xy'] = reg_xy * self.cfgs.REG_WEIGHT
                # self.losses_dict['reg_loss'] = reg_loss * self.cfgs.REG_WEIGHT

        box_pred_list, cls_prob_list, proposal_list = rpn_box_pred_list, rpn_cls_prob_list, anchor_list

        all_box_pred_list, all_cls_prob_list, all_proposal_list = [], [], []

        for i in range(self.cfgs.NUM_REFINE_STAGE):
            box_pred_list, cls_prob_list, proposal_list = self.refine_stage(input_img_batch,
                                                                            gtboxes_batch_r,
                                                                            box_pred_list,
                                                                            cls_prob_list,
                                                                            proposal_list,
                                                                            feature_pyramid,
                                                                            gpu_id,
                                                                            pos_threshold=self.cfgs.REFINE_IOU_POSITIVE_THRESHOLD[i],
                                                                            neg_threshold=self.cfgs.REFINE_IOU_NEGATIVE_THRESHOLD[i],
                                                                            stage='' if i == 0 else '_stage{}'.format(i + 2),
                                                                            proposal_filter=True if i == 0 else False)

            if not self.is_training:
                all_box_pred_list.extend(box_pred_list)
                all_cls_prob_list.extend(cls_prob_list)
                all_proposal_list.extend(proposal_list)
            else:
                all_box_pred_list, all_cls_prob_list, all_proposal_list = box_pred_list, cls_prob_list, proposal_list

        # 5. postprocess
        with tf.variable_scope('postprocess_detctions'):
            box_pred = tf.concat(all_box_pred_list, axis=0)
            cls_prob = tf.concat(all_cls_prob_list, axis=0)
            proposal = tf.concat(all_proposal_list, axis=0)

            boxes, scores, category = self.postprocess_detctions(refine_bbox_pred=box_pred,
                                                                 refine_cls_prob=cls_prob,
                                                                 anchors=proposal,
                                                                 gpu_id=gpu_id)
            boxes = tf.stop_gradient(boxes)
            scores = tf.stop_gradient(scores)
            category = tf.stop_gradient(category)

        if self.is_training:
            return boxes, scores, category, self.losses_dict
        else:
            return boxes, scores, category

    def postprocess_detctions(self, refine_bbox_pred, refine_cls_prob, anchors, gpu_id):

        def filter_detections(boxes, scores):
            """
            :param boxes: [-1, 4]
            :param scores: [-1, ]
            :param labels: [-1, ]
            :return:
            """
            if self.is_training:
                indices = tf.reshape(tf.where(tf.greater(scores, self.cfgs.VIS_SCORE)), [-1, ])
            else:
                indices = tf.reshape(tf.where(tf.greater(scores, self.cfgs.FILTERED_SCORE)), [-1, ])

            if self.cfgs.NMS:
                filtered_boxes = tf.gather(boxes, indices)
                filtered_scores = tf.gather(scores, indices)

                # perform NMS
                max_output_size = 4000 if 'DOTA' in self.cfgs.NET_NAME else 200
                nms_indices = nms_rotate.nms_rotate(decode_boxes=filtered_boxes,
                                                    scores=filtered_scores,
                                                    iou_threshold=self.cfgs.NMS_IOU_THRESHOLD,
                                                    max_output_size=100 if self.is_training else max_output_size,
                                                    use_gpu=True,
                                                    gpu_id=gpu_id)

                # filter indices based on NMS
                indices = tf.gather(indices, nms_indices)

            # add indices to list of all indices
            return indices

        boxes_pred = bbox_transform.rbbox_transform_inv(boxes=anchors, deltas=refine_bbox_pred,
                                                        scale_factors=self.cfgs.ANCHOR_SCALE_FACTORS)

        return_boxes_pred = []
        return_scores = []
        return_labels = []
        for j in range(0, self.cfgs.CLASS_NUM):
            indices = filter_detections(boxes_pred, refine_cls_prob[:, j])
            tmp_boxes_pred = tf.reshape(tf.gather(boxes_pred, indices), [-1, 5])
            tmp_scores = tf.reshape(tf.gather(refine_cls_prob[:, j], indices), [-1, ])

            return_boxes_pred.append(tmp_boxes_pred)
            return_scores.append(tmp_scores)
            return_labels.append(tf.ones_like(tmp_scores) * (j + 1))

        return_boxes_pred = tf.concat(return_boxes_pred, axis=0)
        return_scores = tf.concat(return_scores, axis=0)
        return_labels = tf.concat(return_labels, axis=0)

        return return_boxes_pred, return_scores, return_labels
