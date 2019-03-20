#!/usr/bin/env python3
import argparse
import os
import sys
from datetime import datetime

import cv2
import numpy as np
import tensorflow as tf
import tensorflow.contrib.slim.nets

from image_generator import ImageGenerator
from model import SGRU


def vgg_19_evaluate(image):
    with tf.variable_scope('', reuse=tf.AUTO_REUSE):
        _, end_points = tf.contrib.slim.nets.vgg.vgg_19(image, is_training=False)
    return end_points


def build_loss_func(sgru_model, image_rgb_real, batch_size):

    image_bw = sgru_model.image_bw
    images_rgb_fake = sgru_model.images_rgb_fake

    lambda_weights = [0.88, 0.79, 0.63, 0.51, 0.39, 1.07]

    end_points_real = vgg_19_evaluate(image_rgb_real)

    layers = [
        'input',
        'vgg_19/conv1/conv1_2',
        'vgg_19/conv2/conv2_2',
        'vgg_19/conv3/conv3_2',
        'vgg_19/conv4/conv4_2',
        'vgg_19/conv5/conv5_2',
    ]

    losses = []

    # Iterate through Unet output collection
    collection_size = 9
    for i in range(collection_size):

        loss = tf.Variable(0.0)

        image_rgb_fake = images_rgb_fake[:, :, :, i*3:(i+1)*3]

        end_points_fake = vgg_19_evaluate(image_rgb_fake)

        for weight, layer in zip(lambda_weights, layers):

            if layer == 'input':
                act_fake = image_rgb_fake
                act_real = image_rgb_real
            else:
                act_fake = end_points_fake[layer]
                act_real = end_points_real[layer]

            # Resize image
            mask = tf.image.resize_images(image_bw, tf.shape(act_fake)[1:3])

            loss_inner = tf.multiply(mask, act_fake-act_real)
            loss_inner = tf.norm(loss_inner, ord=1, axis=[1, 2])
            loss_inner = tf.reduce_sum(loss_inner)
            loss_inner = weight * loss_inner

            loss = loss + loss_inner

        losses.append(loss)

    loss = tf.reduce_min(losses)
    tf.summary.scalar('Loss', loss)
    return loss


def save_images(output_fname, batch_rgb_fake, batch_rgb_real, batch_bw):
    """Tile images"""
    batch_fake = (batch_rgb_fake * 255).astype(np.uint8)
    batch_real = (batch_rgb_real * 255).astype(np.uint8)
    batch_bw = (batch_bw * 255).astype(np.uint8)

    row_images = []
    for image_bw, image_rgb_real, collection_fake in zip(batch_bw, batch_real, batch_fake):
        row = [cv2.cvtColor(image_bw, cv2.COLOR_GRAY2BGR), image_rgb_real]
        row += [collection_fake[:, :, i*3:(i+1)*3] for i in range(9)]
        row_image = np.hstack(row)
        row_images.append(row_image)
    out_image = np.vstack(row_images)

    cv2.imwrite(output_fname, out_image)


def train(sgru_model, loss_func, optim_func, image_rgb_real, args):

    # join the log directory with the experiment name
    output_dir = os.path.join(args.output_dir, args.name)

    # Load VGG variables
    variables_to_restore = tf.contrib.framework.get_variables_to_restore()
    vgg_init_fn = tf.contrib.framework.assign_from_checkpoint_fn(args.vgg_ckpt,
                                                                 variables_to_restore,
                                                                 ignore_missing_vars=True)

    # Add an op to initialize the variables.
    init_op = tf.global_variables_initializer()

    with tf.Session() as sess:

        # Summary operations for tensorboard
        summary_op = tf.summary.merge_all()
        writer = tf.summary.FileWriter(output_dir, graph=sess.graph)

        # Initialize all variables
        sess.run(init_op)
        # Initialize VGG variables (these were reset during global initialization)
        vgg_init_fn(sess)

        losses = []

        image_dir = os.path.join(args.data_dir, 'images')
        image_generator = ImageGenerator(image_dir, args.batch_size, args.num_cpus)

        batch_bw_op, batch_rgb_op = image_generator.load_batches()

        for epoch in range(args.epochs):

            for batch_num in range(image_generator.num_batches):

                batch_bw, batch_rgb = sess.run([batch_bw_op, batch_rgb_op])

                feed_dict = {
                    sgru_model.image_bw: batch_bw,
                    image_rgb_real: batch_rgb
                }
                out_list = [sgru_model.images_rgb_fake, loss_func, optim_func, summary_op]
                images_rgb_fake_out, loss, _ , summary = sess.run(out_list, feed_dict=feed_dict)

                # Report to tensorboard all the summaries at the current timestep
                writer.add_summary(summary, epoch*image_generator.num_batches + batch_num)

                print('Epoch {}, batch number: {}, loss: {}'.format(epoch, batch_num, loss))

                losses.append(loss)

                if batch_num % args.save_every == 0:
                    output_fname = os.path.join(output_dir, 'images/{}_{}.jpg'.format(epoch, batch_num))
                    save_images(output_fname, images_rgb_fake_out, batch_rgb, batch_bw)
                    sgru_model.save(os.path.join(output_dir, 'model.ckpt'))


def main(args):

    if not os.path.isfile(os.path.join(args.data_dir, 'vgg_19.ckpt')):
        sys.exit('Download VGG19 checkpoint from ' +
                 'http://download.tensorflow.org/models/vgg_19_2016_08_28.tar.gz ' +
                 'and save it to the root of your data_dir')

    # If an experiment name isn't provided, we set it to a timestamp
    if not args.name:
        args.name = datetime.now().strftime("sgru-%Y-%m-%d-%H-%M-%S-%f")

    image_rgb_real = tf.placeholder(tf.float32, shape=[None, None, None, 3], name='img_real')
    model = SGRU()

    loss_func = build_loss_func(model, image_rgb_real, args.batch_size)
    optimizer_func = tf.train.AdamOptimizer(learning_rate=0.00005).minimize(loss_func)

    train(model, loss_func, optimizer_func, image_rgb_real, args)


def get_args():
    parser = argparse.ArgumentParser()
    parser.add_argument('data_dir', help='Directory containing image subdirs')
    parser.add_argument('output_dir', help='Output directory')
    parser.add_argument('--epochs', type=int, default=100, help='Number of epochs')
    parser.add_argument('--batch-size', type=int, default=4, help='Batch size')
    parser.add_argument('--resume', action='store_true', help='Resume training models')
    parser.add_argument('--save-every', type=int, default=1, help='Save image every n iterations')
    parser.add_argument('--num-cpus', type=int, default=4, help='Num CPUs to load images with')
    parser.add_argument('--name', type=str, default=None, help='Name of the experiment (defaults to timestamp)')
    return parser.parse_args()


if __name__ == '__main__':
    args = get_args()
    main(args)
