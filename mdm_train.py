from datetime import datetime
import data_provider
import mdm_model
import numpy as np
import os
from pathlib import Path
import tensorflow as tf
import time
import utils
import menpo
import menpo.io as mio
from menpo.shape.pointcloud import PointCloud

FLAGS = tf.app.flags.FLAGS
tf.app.flags.DEFINE_float('lr', 0.001, """Initial learning rate.""")
tf.app.flags.DEFINE_float('lr_decay_steps', 15000, """Learning rate decay steps.""")
tf.app.flags.DEFINE_float('lr_decay_rate', 0.1, """Learning rate decay rate.""")
tf.app.flags.DEFINE_integer('batch_size', 60, """The batch size to use.""")
tf.app.flags.DEFINE_integer('num_threads', 4, """How many pre-process threads to use.""")
tf.app.flags.DEFINE_string('train_dir', 'ckpt/train', """Log out directory.""")
tf.app.flags.DEFINE_string('pre_trained_dir', '', """Restore pre-trained model.""")
tf.app.flags.DEFINE_integer('max_steps', 100000, """Number of batches to run.""")
tf.app.flags.DEFINE_string('train_device', '/gpu:0', """Device to train with.""")
tf.app.flags.DEFINE_string(
    'datasets',
    ':'.join(
        ('Dataset/LFPW/trainset/Images/*.png',
         'Dataset/AFW/Images/*.jpg',
         'Dataset/HELEN/trainset/Images/*.jpg'
         )
    ),
    """Directory where to write event logs and checkpoint."""
)
tf.app.flags.DEFINE_integer('num_patches', 68, 'Landmark number')
tf.app.flags.DEFINE_integer('patch_size', 30, 'The extracted patch size')

# The decay to use for the moving average.
MOVING_AVERAGE_DECAY = 0.9999


def train(scope=''):
    """Train on dataset for a number of steps."""
    with tf.Graph().as_default(), tf.device('/gpu:0'):
        # Global steps
        tf_global_step = tf.get_variable(
            'global_step', [],
            initializer=tf.constant_initializer(0),
            trainable=False
        )

        # Learning rate
        tf_lr = tf.train.exponential_decay(
            FLAGS.lr,
            tf_global_step,
            FLAGS.lr_decay_steps,
            FLAGS.lr_decay_rate,
            staircase=True,
            name='learning_rate'
        )
        tf.summary.scalar('learning_rate', tf_lr)

        # Create an optimizer that performs gradient descent.
        opt = tf.train.AdamOptimizer(tf_lr)

        train_dirs = FLAGS.datasets.split(':')
        _image_paths, _image_shape, _mean_shape, _pca_model = \
            data_provider.preload_images(train_dirs, verbose=True)
        assert(_mean_shape.shape[0] == FLAGS.num_patches)

        tf_mean_shape = tf.constant(_mean_shape, dtype=tf.float32, name='mean_shape')

        def get_random_sample(rotation_stddev=10):
            # Read a random image with landmarks and bb
            random_idx = np.random.randint(low=0, high=len(_image_paths))
            im = mio.import_image(_image_paths[random_idx])
            bb_root = im.path.parent.parent
            im.landmarks['bb'] = mio.import_landmark_file(
                str(Path(bb_root / 'BoundingBoxes' / (im.path.stem + '.pts')))
            )

            # Align to the same space with mean shape
            im = im.crop_to_landmarks_proportion(0.3, group='bb')
            im = im.rescale_to_pointcloud(PointCloud(_mean_shape), group='PTS')
            im = data_provider.grey_to_rgb(im)

            # Padding to the same size
            pim = menpo.image.Image(np.random.rand(*_image_shape).astype(np.float32), copy=False)
            height, width = im.pixels.shape[1:]  # im[C, H, W]
            dy = max(int((_image_shape[1] - height - 1) / 2), 0)
            dx = max(int((_image_shape[2] - width - 1) / 2), 0)
            pts = np.copy(im.landmarks['PTS'].points)
            pts[:, 0] += dy
            pts[:, 1] += dx
            pim.pixels[:, dy:(height + dy), dx:(width + dx)] = im.pixels
            pim.landmarks['PTS'] = PointCloud(pts)

            if np.random.rand() < .5:
                pim = utils.mirror_image(pim)
            if np.random.rand() < .5:
                theta = np.random.normal(scale=rotation_stddev)
                rot = menpo.transform.rotate_ccw_about_centre(pim.landmarks['PTS'], theta)
                pim = pim.warp_to_shape(pim.shape, rot)

            random_image = pim.pixels.transpose(1, 2, 0).astype('float32')
            random_shape = pim.landmarks['PTS'].points.astype('float32')
            return random_image, random_shape

        with tf.name_scope('data_provider', values=[tf_mean_shape]):
            tf_image, tf_shape = tf.py_func(
                get_random_sample, [], [tf.float32, tf.float32],
                stateful=True,
                name='random_sample'
            )
            tf_initial_shape = data_provider.random_shape(tf_shape, tf_mean_shape, _pca_model)
            tf_image.set_shape(_image_shape[1:] + _image_shape[:1])
            tf_shape.set_shape(_mean_shape.shape)
            tf_initial_shape.set_shape(_mean_shape.shape)
            tf_image = data_provider.distort_color(tf_image)

            tf_images, tf_shapes, tf_initial_shapes = tf.train.batch(
                [tf_image, tf_shape, tf_initial_shape],
                FLAGS.batch_size,
                dynamic_pad=False,
                capacity=5000,
                enqueue_many=False,
                num_threads=FLAGS.num_threads,
                name='batch'
            )

        print('Defining model...')
        with tf.device(FLAGS.train_device):
            tf_model = mdm_model.MDMModel(
                tf_images,
                tf_shapes,
                tf_initial_shapes,
                num_iterations=4,
                num_patches=FLAGS.num_patches,
                patch_shape=(FLAGS.patch_size, FLAGS.patch_size)
            )
            with tf.name_scope('losses', values=tf_model.dxs + [tf_initial_shapes, tf_shapes]):
                tf_total_loss = 0
                for i, tf_dx in enumerate(tf_model.dxs):
                    with tf.name_scope('step{}'.format(i)):
                        tf_norm_error = mdm_model.normalized_rmse(
                            tf_dx + tf_initial_shapes,
                            tf_shapes,
                            num_patches=FLAGS.num_patches
                        )
                        tf_loss = tf.reduce_mean(tf_norm_error)
                    tf.summary.scalar('losses/step_{}'.format(i), tf_loss)
                    tf_total_loss += tf_loss
            tf.summary.scalar('losses/total', tf_total_loss)
            # Calculate the gradients for the batch of data
            tf_grads = opt.compute_gradients(tf_total_loss)
        tf.summary.histogram('dx', tf_model.dx)

        bn_updates = tf.get_collection(tf.GraphKeys.UPDATE_OPS, scope)

        # Add histograms for gradients.
        for grad, var in tf_grads:
            if grad is not None:
                tf.summary.histogram(var.op.name + '/gradients', grad)

        # Apply the gradients to adjust the shared variables.
        with tf.name_scope('Optimizer', values=[tf_grads, tf_global_step]):
            apply_gradient_op = opt.apply_gradients(tf_grads, global_step=tf_global_step)

        # Add histograms for trainable variables.
        for var in tf.trainable_variables():
            tf.summary.histogram(var.op.name, var)

        # Track the moving averages of all trainable variables.
        # Note that we maintain a "double-average" of the BatchNormalization
        # global statistics. This is more complicated then need be but we employ
        # this for backward-compatibility with our previous models.
        with tf.name_scope('MovingAverage', values=[tf_global_step]):
            variable_averages = tf.train.ExponentialMovingAverage(MOVING_AVERAGE_DECAY, tf_global_step)
            variables_to_average = (tf.trainable_variables() + tf.moving_average_variables())
            variables_averages_op = variable_averages.apply(variables_to_average)

        # Group all updates to into a single train op.
        # NOTE: Currently we are not using batchnorm in MDM.
        bn_updates_op = tf.group(*bn_updates, name='bn_group')
        train_op = tf.group(
            apply_gradient_op, variables_averages_op, bn_updates_op,
            name='train_group'
        )

        # Create a saver.
        saver = tf.train.Saver()

        # Build the summary operation from the last tower summaries.
        summary_op = tf.summary.merge_all()
        # Start running operations on the Graph. allow_soft_placement must be
        # set to True to build towers on GPU, as some of the ops do not have GPU
        # implementations.
        config = tf.ConfigProto(allow_soft_placement=True)
        config.gpu_options.allow_growth = True
        sess = tf.Session(config=config)
        # Build an initialization operation to run below.
        init = tf.global_variables_initializer()
        print('Initializing variables...')
        sess.run(init)
        print('Initialized variables.')

        if FLAGS.pre_trained_dir:
            assert tf.gfile.Exists(FLAGS.pre_trained_dir)
            restorer = tf.train.Saver()
            restorer.restore(sess, FLAGS.pre_trained_dir)
            print('%s: Pre-trained model restored from %s' %
                  (datetime.now(), FLAGS.pre_trained_dir))

        # Start the queue runners.
        tf.train.start_queue_runners(sess=sess)

        summary_writer = tf.summary.FileWriter(FLAGS.train_dir, sess.graph)

        print('Starting training...')
        for step in range(FLAGS.max_steps):
            start_time = time.time()
            _, loss_value = sess.run([train_op, tf_total_loss])
            duration = time.time() - start_time

            assert not np.isnan(loss_value), 'Model diverged with loss = NaN'

            if step % 100 == 0:
                examples_per_sec = FLAGS.batch_size / float(duration)
                format_str = (
                    '%s: step %d, loss = %.2f (%.1f examples/sec; %.3f '
                    'sec/batch)')
                print(format_str % (datetime.now(), step, loss_value,
                                    examples_per_sec, duration))

            if step % 200 == 0:
                summary_str = sess.run(summary_op)
                summary_writer.add_summary(summary_str, step)

            # Save the model checkpoint periodically.
            if step % 1000 == 0 or (step + 1) == FLAGS.max_steps:
                checkpoint_path = os.path.join(FLAGS.train_dir, 'model.ckpt')
                saver.save(sess, checkpoint_path, global_step=step)


if __name__ == '__main__':
    train()
