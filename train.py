#!/usr/bin/env python3
import json
import logging.config
import os
import sys
import time
from argparse import ArgumentParser
from datetime import timedelta
from importlib import import_module
from signal import SIGINT, SIGTERM

import numpy as np
import tensorflow as tf
import tensorflow_addons as tfa

import common
import lbtoolbox as lb
import loss
from heads import HEAD_CHOICES
from nets import NET_CHOICES

# os.environ["CUDA_VISIBLE_DEVICES"] = "-1"

tf.compat.v1.disable_eager_execution()

parser = ArgumentParser(description='Train a ReID network.')

# Required.

parser.add_argument(
    '--experiment_root', required=True, type=common.writeable_directory,
    help='Location used to store checkpoints and dumped data.')

parser.add_argument(
    '--train_set',
    help='Path to the train_set csv file.')

parser.add_argument(
    '--test_set',
    help='Path to the test_set csv file.')

parser.add_argument(
    '--image_root', type=common.readable_directory,
    help='Path that will be pre-pended to the filenames in the train_set csv.')

# Optional with sane defaults.

parser.add_argument(
    '--resume', action='store_true', default=False,
    help='When this flag is provided, all other arguments apart from the '
         'experiment_root are ignored and a previously saved set of arguments '
         'is loaded.')

parser.add_argument(
    '--model_name', default='resnet_v1_50', choices=NET_CHOICES,
    help='Name of the model to use.')

parser.add_argument(
    '--head_name', default='fc1024', choices=HEAD_CHOICES,
    help='Name of the head to use.')

parser.add_argument(
    '--embedding_dim', default=128, type=common.positive_int,
    help='Dimensionality of the embedding space.')

parser.add_argument(
    '--initial_checkpoint', default=None,
    help='Path to the checkpoint file of the pretrained network.')

# TODO move these defaults to the .sh script?
parser.add_argument(
    '--batch_p', default=32, type=common.positive_int,
    help='The number P used in the PK-batches')

parser.add_argument(
    '--batch_k', default=4, type=common.positive_int,
    help='The numberK used in the PK-batches')

parser.add_argument(
    '--net_input_height', default=256, type=common.positive_int,
    help='Height of the input directly fed into the network.')

parser.add_argument(
    '--net_input_width', default=128, type=common.positive_int,
    help='Width of the input directly fed into the network.')

parser.add_argument(
    '--pre_crop_height', default=288, type=common.positive_int,
    help='Height used to resize a loaded image. This is ignored when no crop '
         'augmentation is applied.')

parser.add_argument(
    '--pre_crop_width', default=144, type=common.positive_int,
    help='Width used to resize a loaded image. This is ignored when no crop '
         'augmentation is applied.')
# TODO end

parser.add_argument(
    '--loading_threads', default=8, type=common.positive_int,
    help='Number of threads used for parallel loading.')

parser.add_argument(
    '--margin', default='soft', type=common.float_or_string,
    help='What margin to use: a float value for hard-margin, "soft" for '
         'soft-margin, or no margin if "none".')

parser.add_argument(
    '--metric', default='euclidean', choices=loss.cdist.supported_metrics,
    help='Which metric to use for the distance between embeddings.')

parser.add_argument(
    '--loss', default='batch_hard', choices=loss.LOSS_CHOICES.keys(),
    help='Enable the super-mega-advanced top-secret sampling stabilizer.')

parser.add_argument(
    '--learning_rate', default=3e-4, type=common.positive_float,
    help='The initial value of the learning-rate, before it kicks in.')

parser.add_argument(
    '--train_iterations', default=25000, type=common.positive_int,
    help='Number of training iterations.')

parser.add_argument(
    '--decay_start_iteration', default=15000, type=int,
    help='At which iteration the learning-rate decay should kick-in.'
         'Set to -1 to disable decay completely.')

parser.add_argument(
    '--checkpoint_frequency', default=1000, type=common.nonnegative_int,
    help='After how many iterations a checkpoint is stored. Set this to 0 to '
         'disable intermediate storing. This will result in only one final '
         'checkpoint.')

parser.add_argument(
    '--flip_augment', action='store_true', default=False,
    help='When this flag is provided, flip augmentation is performed.')

parser.add_argument(
    '--rotate_augment', action='store_true', default=False,
    help='When this flag is provided, rotate augmentation is performed.')

parser.add_argument(
    '--crop_augment', action='store_true', default=False,
    help='When this flag is provided, crop augmentation is performed. Based on'
         'The `crop_height` and `crop_width` parameters. Changing this flag '
         'thus likely changes the network input size!')

parser.add_argument(
    '--detailed_logs', action='store_true', default=False,
    help='Store very detailed logs of the training in addition to TensorBoard'
         ' summaries. These are mem-mapped numpy files containing the'
         ' embeddings, losses and FIDs seen in each batch during training.'
         ' Everything can be re-constructed and analyzed that way.')


def sample_k_fids_for_pid(pid, all_fids, all_pids, batch_k):
    """ Given a PID, select K FIDs of that specific PID. """
    possible_fids = tf.boolean_mask(tensor=all_fids, mask=tf.equal(all_pids, pid))

    # The following simply uses a subset of K of the possible FIDs
    # if more than, or exactly K are available. Otherwise, we first
    # create a padded list of indices which contain a multiple of the
    # original FID count such that all of them will be sampled equally likely.
    count = tf.shape(input=possible_fids)[0]
    padded_count = tf.cast(tf.math.ceil(batch_k / tf.cast(count, tf.float32)), tf.int32) * count
    full_range = tf.math.mod(tf.range(padded_count), count)

    # Sampling is always performed by shuffling and taking the first k.
    shuffled = tf.random.shuffle(full_range)
    selected_fids = tf.gather(possible_fids, shuffled[:batch_k])

    return selected_fids, tf.fill([batch_k], pid)


def main():
    args = parser.parse_args()

    # We store all arguments in a json file. This has two advantages:
    # 1. We can always get back and see what exactly that experiment was
    # 2. We can resume an experiment as-is without needing to remember all flags.
    args_file = os.path.join(args.experiment_root, 'args.json')
    if args.resume:
        if not os.path.isfile(args_file):
            raise IOError('`args.json` not found in {}'.format(args_file))

        print('Loading args from {}.'.format(args_file))
        with open(args_file, 'r') as f:
            args_resumed = json.load(f)
        args_resumed['resume'] = True  # This would be overwritten.

        # When resuming, we not only want to populate the args object with the
        # values from the file, but we also want to check for some possible
        # conflicts between loaded and given arguments.
        for key, value in args.__dict__.items():
            if key in args_resumed:
                resumed_value = args_resumed[key]
                if resumed_value != value:
                    print('Warning: For the argument `{}` we are using the'
                          ' loaded value `{}`. The provided value was `{}`'
                          '.'.format(key, resumed_value, value))
                    args.__dict__[key] = resumed_value
            else:
                print('Warning: A new argument was added since the last run:'
                      ' `{}`. Using the new value: `{}`.'.format(key, value))

    else:
        # If the experiment directory exists already, we bail in fear.
        if os.path.exists(args.experiment_root):
            if os.listdir(args.experiment_root):
                print('The directory {} already exists and is not empty.'
                      ' If you want to resume training, append --resume to'
                      ' your call.'.format(args.experiment_root))
                exit(1)
        else:
            os.makedirs(args.experiment_root)

        # Store the passed arguments for later resuming and grepping in a nice
        # and readable format.
        with open(args_file, 'w') as f:
            json.dump(vars(args), f, ensure_ascii=False, indent=2, sort_keys=True)

    log_file = os.path.join(args.experiment_root, "train")
    logging.config.dictConfig(common.get_logging_dict(log_file))
    log = logging.getLogger('train')

    # Also show all parameter values at the start, for ease of reading logs.
    log.info('Training using the following parameters:')
    for key, value in sorted(vars(args).items()):
        log.info('{}: {}'.format(key, value))

    # Check them here, so they are not required when --resume-ing.
    if not args.train_set:
        parser.print_help()
        log.error("You did not specify the `train_set` argument!")
        sys.exit(1)
    if not args.image_root:
        parser.print_help()
        log.error("You did not specify the required `image_root` argument!")
        sys.exit(1)

    # Load the data from the CSV file.
    pids, fids = common.load_dataset(args.train_set, args.image_root)
    max_fid_len = max(map(len, fids))  # We'll need this later for logfiles.

    # Setup a tf.Dataset where one "epoch" loops over all PIDS.
    # PIDS are shuffled after every epoch and continue indefinitely.
    unique_pids = np.unique(pids)
    dataset = tf.data.Dataset.from_tensor_slices(unique_pids)
    dataset = dataset.shuffle(len(unique_pids))

    # Constrain the dataset size to a multiple of the batch-size, so that
    # we don't get overlap at the end of each epoch.
    dataset = dataset.take((len(unique_pids) // args.batch_p) * args.batch_p)
    dataset = dataset.repeat(None)  # Repeat forever. Funny way of stating it.

    # For every PID, get K images.
    dataset = dataset.map(lambda pid: sample_k_fids_for_pid(
        pid, all_fids=fids, all_pids=pids, batch_k=args.batch_k))

    # Ungroup/flatten the batches for easy loading of the files.
    dataset = dataset.unbatch()

    # Convert filenames to actual image tensors.
    net_input_size = (args.net_input_height, args.net_input_width)
    pre_crop_size = (args.pre_crop_height, args.pre_crop_width)
    dataset = dataset.map(
        lambda fid, pid: common.fid_to_image(
            fid, pid, image_root=args.image_root,
            image_size=pre_crop_size if args.crop_augment else net_input_size),
        num_parallel_calls=args.loading_threads)

    # Augment the data if specified by the arguments.
    if args.flip_augment:
        random_angles = tf.random.uniform(shape=(), minval=0, maxval=1, dtype=tf.int32) * 2
        dataset = dataset.map(
            lambda im, fid, pid: (tf.image.rot90(im, k=random_angles), fid, pid))
    if args.rotate_augment:
        random_angles = tf.random.uniform(shape=(), minval=-np.pi / 4, maxval=np.pi / 4)
        dataset = dataset.map(
            lambda im, fid, pid: (tfa.image.rotate(im, random_angles), fid, pid))
    if args.crop_augment:
        dataset = dataset.map(
            lambda im, fid, pid: (tf.image.random_crop(im, net_input_size + (3,)), fid, pid))

    # Group it back into PK batches.
    batch_size = args.batch_p * args.batch_k
    dataset = dataset.batch(batch_size)

    # Overlap producing and consuming for parallelism.
    dataset = dataset.prefetch(1)

    # Since we repeat the data infinitely, we only need a one-shot iterator.
    dataset_iterator = tf.compat.v1.data.make_initializable_iterator(dataset)
    images, fids, pids = dataset_iterator.get_next()
    images_ph = tf.compat.v1.placeholder(images.dtype, shape=images.get_shape())
    pids_ph = tf.compat.v1.placeholder(pids.dtype, shape=pids.get_shape())

    test_images = None
    if args.test_set:
        # Load the data from the CSV file.
        test_pids, test_fids = common.load_dataset(args.test_set, args.image_root)

        # Setup a tf.Dataset where one "epoch" loops over all PIDS.
        # PIDS are shuffled after every epoch and continue indefinitely.
        test_unique_pids = np.unique(test_pids)
        test_dataset = tf.data.Dataset.from_tensor_slices(test_unique_pids)
        test_dataset = test_dataset.shuffle(len(test_unique_pids))

        # Constrain the dataset size to a multiple of the batch-size, so that
        # we don't get overlap at the end of each epoch.
        test_dataset = test_dataset.take((len(test_unique_pids) // args.batch_p) * args.batch_p)
        test_dataset = test_dataset.repeat(None)  # Repeat forever. Funny way of stating it.

        # For every PID, get K images.
        test_dataset = test_dataset.map(lambda pid: sample_k_fids_for_pid(
            pid, all_fids=test_fids, all_pids=test_pids, batch_k=args.batch_k))

        # Ungroup/flatten the batches for easy loading of the files.
        test_dataset = test_dataset.unbatch()

        # Convert filenames to actual image tensors.
        net_input_size = (args.net_input_height, args.net_input_width)
        pre_crop_size = (args.pre_crop_height, args.pre_crop_width)
        test_dataset = test_dataset.map(
            lambda fid, pid: common.fid_to_image(
                fid, pid, image_root=args.image_root,
                image_size=pre_crop_size if args.crop_augment else net_input_size),
            num_parallel_calls=args.loading_threads)

        # Group it back into PK batches.
        test_batch_size = args.batch_p * args.batch_k
        test_dataset = test_dataset.batch(test_batch_size)

        # Overlap producing and consuming for parallelism.
        test_dataset = test_dataset.prefetch(1)

        # Since we repeat the data infinitely, we only need a one-shot iterator.
        test_images, test_fids, test_pids = tf.compat.v1.data.make_one_shot_iterator(test_dataset).get_next()

    # Create the model and an embedding head.
    model = import_module('nets.' + args.model_name)
    head = import_module('heads.' + args.head_name)

    # Feed the image through the model. The returned `body_prefix` will be used
    # further down to load the pre-trained weights for all variables with this
    # prefix.
    endpoints, body_prefix = model.endpoints(images_ph, is_training=True)
    with tf.compat.v1.name_scope('head'):
        endpoints = head.head(endpoints, args.embedding_dim, is_training=True)

    # Create the loss in two steps:
    # 1. Compute all pairwise distances according to the specified metric.
    # 2. For each anchor along the first dimension, compute its loss.
    dists = loss.cdist(endpoints['emb'], endpoints['emb'], metric=args.metric)
    losses, train_top1, prec_at_k, _, neg_dists, pos_dists = loss.LOSS_CHOICES[args.loss](
        dists, pids_ph, args.margin, batch_precision_at_k=args.batch_k - 1)

    # Count the number of active entries, and compute the total batch loss.
    num_active = tf.reduce_sum(input_tensor=tf.cast(tf.greater(losses, 1e-5), tf.float32))
    loss_mean = tf.reduce_mean(input_tensor=losses)

    # Some logging for tensorboard.
    tf.compat.v1.summary.histogram('loss_distribution', losses)
    tf.compat.v1.summary.scalar('loss', loss_mean)
    tf.compat.v1.summary.scalar('batch_top1', train_top1)
    tf.compat.v1.summary.scalar('batch_prec_at_{}'.format(args.batch_k - 1), prec_at_k)
    tf.compat.v1.summary.scalar('active_count', num_active)
    tf.compat.v1.summary.histogram('embedding_dists', dists)
    tf.compat.v1.summary.histogram('embedding_pos_dists', pos_dists)
    tf.compat.v1.summary.histogram('embedding_neg_dists', neg_dists)
    tf.compat.v1.summary.histogram('embedding_lengths',
                                   tf.norm(tensor=endpoints['emb_raw'], axis=1))

    # Create the mem-mapped arrays in which we'll log all training detail in
    # addition to tensorboard, because tensorboard is annoying for detailed
    # inspection and actually discards data in histogram summaries.
    if args.detailed_logs:
        log_embs = lb.create_or_resize_dat(
            os.path.join(args.experiment_root, 'embeddings'),
            dtype=np.float32, shape=(args.train_iterations, batch_size, args.embedding_dim))
        log_loss = lb.create_or_resize_dat(
            os.path.join(args.experiment_root, 'losses'),
            dtype=np.float32, shape=(args.train_iterations, batch_size))
        log_fids = lb.create_or_resize_dat(
            os.path.join(args.experiment_root, 'fids'),
            dtype='S' + str(max_fid_len), shape=(args.train_iterations, batch_size))
        if test_images:
            log_val_embs = lb.create_or_resize_dat(
                os.path.join(args.experiment_root, 'val_embeddings'),
                dtype=np.float32, shape=(args.train_iterations, batch_size, args.embedding_dim))
            log_val_loss = lb.create_or_resize_dat(
                os.path.join(args.experiment_root, 'val_losses'),
                dtype=np.float32, shape=(args.train_iterations, batch_size))
            log_val_fids = lb.create_or_resize_dat(
                os.path.join(args.experiment_root, 'val_fids'),
                dtype='S' + str(max_fid_len), shape=(args.train_iterations, batch_size))

    # These are collected here before we add the optimizer, because depending
    # on the optimizer, it might add extra slots, which are also global
    # variables, with the exact same prefix.
    model_variables = tf.compat.v1.get_collection(
        tf.compat.v1.GraphKeys.GLOBAL_VARIABLES, body_prefix)

    # Define the optimizer and the learning-rate schedule.
    # Unfortunately, we get NaNs if we don't handle no-decay separately.
    global_step = tf.Variable(0, name='global_step', trainable=False)
    if 0 <= args.decay_start_iteration < args.train_iterations:
        learning_rate = tf.compat.v1.train.exponential_decay(
            args.learning_rate,
            tf.maximum(0, global_step - args.decay_start_iteration),
            args.train_iterations - args.decay_start_iteration, 0.001)
    else:
        learning_rate = args.learning_rate
    tf.compat.v1.summary.scalar('learning_rate', learning_rate)
    optimizer = tf.compat.v1.train.AdamOptimizer(learning_rate)
    # Feel free to try others!
    # optimizer = tf.train.AdadeltaOptimizer(learning_rate)

    # Update_ops are used to update batchnorm stats.
    with tf.control_dependencies(tf.compat.v1.get_collection(tf.compat.v1.GraphKeys.UPDATE_OPS)):
        train_op = optimizer.minimize(loss_mean, global_step=global_step)

    # Define a saver for the complete model.
    checkpoint_saver = tf.compat.v1.train.Saver(max_to_keep=0)

    with tf.compat.v1.Session() as sess:
        if args.resume:
            # In case we're resuming, simply load the full checkpoint to init.
            last_checkpoint = tf.train.latest_checkpoint(args.experiment_root)
            log.info('Restoring from checkpoint: {}'.format(last_checkpoint))
            checkpoint_saver.restore(sess, last_checkpoint)
        else:
            # But if we're starting from scratch, we may need to load some
            # variables from the pre-trained weights, and random init others.
            sess.run(tf.compat.v1.global_variables_initializer())
            if args.initial_checkpoint is not None:
                saver = tf.compat.v1.train.Saver(model_variables)
                saver.restore(sess, args.initial_checkpoint)

            # In any case, we also store this initialization as a checkpoint,
            # such that we could run exactly reproduceable experiments.
            checkpoint_saver.save(sess, os.path.join(
                args.experiment_root, 'checkpoint'), global_step=0)

        merged_summary = tf.compat.v1.summary.merge_all()
        summary_writer = tf.compat.v1.summary.FileWriter(os.path.join(args.experiment_root, 'train'), sess.graph)
        test_summary_writer = tf.compat.v1.summary.FileWriter(os.path.join(args.experiment_root, 'validation'), sess.graph)
        sess.run(dataset_iterator.initializer)

        start_step = sess.run(global_step)
        log.info('Starting training from iteration {}.'.format(start_step))

        # Finally, here comes the main-loop. This `Uninterrupt` is a handy
        # utility such that an iteration still finishes on Ctrl+C and we can
        # stop the training cleanly.
        with lb.Uninterrupt(sigs=[SIGINT, SIGTERM], verbose=True) as u:
            for i in range(start_step, args.train_iterations):

                # Compute gradients, update weights, store logs!
                start_time = time.time()
                images_val, pids_val = sess.run([images, pids])
                _, summary, step, b_prec_at_k, b_embs, b_loss, b_fids = \
                    sess.run([train_op, merged_summary, global_step, prec_at_k, endpoints['emb'], losses, fids], {images_ph: images_val, pids_ph: pids_val})

                test_summary = None
                if test_images != None:
                    images_val, pids_val = sess.run([test_images, test_pids])
                    test_summary, test_b_prec_at_k, test_b_embs, test_b_loss, test_b_fids = \
                        sess.run([merged_summary, prec_at_k, endpoints['emb'], losses, test_fids], {images_ph: images_val, pids_ph: pids_val})

                elapsed_time = time.time() - start_time

                # Compute the iteration speed and add it to the summary.
                # We did observe some weird spikes that we couldn't track down.
                summary2 = tf.compat.v1.Summary()
                summary2.value.add(tag='secs_per_iter', simple_value=elapsed_time)
                summary_writer.add_summary(summary2, step)
                summary_writer.add_summary(summary, step)
                if test_summary != None:
                    test_summary_writer.add_summary(test_summary, step)

                if args.detailed_logs:
                    log_embs[i], log_loss[i], log_fids[i] = b_embs, b_loss, b_fids
                    if test_summary:
                        log_val_embs[i], log_val_loss[i], log_val_fids[i] = test_b_embs, test_b_loss, test_b_fids

                # Do a huge print out of the current progress.
                seconds_todo = (args.train_iterations - step) * elapsed_time
                if test_summary:
                    log.info('iter:{:6d}, loss min|avg|max: {:.3f}|{:.3f}|{:.3f}, '
                             'batch-p@{}: {:.2%}, val_loss min|avg|max: {:.3f}|{:.3f}|{:.3f}, '
                             'batch-p@{}: {:.2%}, ETA: {} ({:.2f}s/it)'.format(
                                 step,
                                 float(np.min(b_loss)),
                                 float(np.mean(b_loss)),
                                 float(np.max(b_loss)),
                                 args.batch_k - 1, float(b_prec_at_k),
                                 float(np.min(test_b_loss)),
                                 float(np.mean(test_b_loss)),
                                 float(np.max(test_b_loss)),
                                 args.batch_k - 1, float(test_b_prec_at_k),
                                 timedelta(seconds=int(seconds_todo)),
                                 elapsed_time))
                else:
                    log.info('iter:{:6d}, loss min|avg|max: {:.3f}|{:.3f}|{:6.3f}, '
                             'batch-p@{}: {:.2%}, ETA: {} ({:.2f}s/it)'.format(
                                 step,
                                 float(np.min(b_loss)),
                                 float(np.mean(b_loss)),
                                 float(np.max(b_loss)),
                                 args.batch_k - 1, float(b_prec_at_k),
                                 timedelta(seconds=int(seconds_todo)),
                                 elapsed_time))
                sys.stdout.flush()
                sys.stderr.flush()

                # Save a checkpoint of training every so often.
                if (args.checkpoint_frequency > 0 and
                        step % args.checkpoint_frequency == 0):
                    checkpoint_saver.save(sess, os.path.join(
                        args.experiment_root, 'checkpoint'), global_step=step)

                # Stop the main-loop at the end of the step, if requested.
                if u.interrupted:
                    log.info("Interrupted on request!")
                    break

        # Store one final checkpoint. This might be redundant, but it is crucial
        # in case intermediate storing was disabled and it saves a checkpoint
        # when the process was interrupted.
        checkpoint_saver.save(sess, os.path.join(
            args.experiment_root, 'checkpoint'), global_step=step)


if __name__ == '__main__':
    main()
