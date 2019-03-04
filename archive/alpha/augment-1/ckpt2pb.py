import tensorflow as tf
import mdm_model
from pathlib import Path
import menpo.io as mio
import numpy as np
import data_provider
import json

tf.flags.DEFINE_string('c', 'config.json', """Model config file""")
with open(tf.flags.FLAGS.c, 'r') as g_config:
    g_config = json.load(g_config)
for k in g_config:
    print(k, type(g_config[k]), g_config[k])
input('OK?(Y/N): ')


def ckpt_pb(pb_path):
    tf.reset_default_graph()
    config = tf.ConfigProto(allow_soft_placement=True)
    config.gpu_options.allow_growth = True
    with tf.Session() as sess:
        path_base = Path(g_config['eval_dataset']).parent.parent
        _mean_shape = mio.import_pickle(path_base / 'reference_shape.pkl')
        _mean_shape = data_provider.align_reference_shape_to_112(_mean_shape)
        _mean_shape = np.expand_dims(_mean_shape, 0)
        assert isinstance(_mean_shape, np.ndarray)
        print(_mean_shape.shape)

        tf_img = tf.placeholder(dtype=tf.float32, shape=(1, 112, 112, 3), name='inputs/input_img')
        tf_dummy = tf.placeholder(dtype=tf.float32, shape=(1, 73, 2), name='inputs/input_shape')
        tf_shape = tf.constant(_mean_shape, dtype=tf.float32, name='MeanShape')

        model = mdm_model.MDMModel(
            tf_img,
            tf_dummy,
            tf_shape,
            batch_size=1,
            num_iterations=g_config['num_iterations'],
            num_patches=g_config['num_patches'],
            patch_shape=(g_config['patch_size'], g_config['patch_size']),
            num_channels=3,
            is_training=False
        )
        output = model.prediction

        saver = tf.train.Saver()
        ckpt = tf.train.get_checkpoint_state(g_config['train_dir'])
        if ckpt and ckpt.model_checkpoint_path:
            saver.restore(sess, ckpt.model_checkpoint_path)
            global_step = ckpt.model_checkpoint_path.split('/')[-1].split('-')[-1]
            print('Successfully loaded model from {} at step={}.'.format(ckpt.model_checkpoint_path, global_step))
        else:
            print('No checkpoint file found')
            return

        output_graph_def = tf.graph_util.convert_variables_to_constants(
            sess, tf.get_default_graph().as_graph_def(), ['Network/Predict/add']
        )

        with tf.gfile.FastGFile(pb_path, mode='wb') as f:
            f.write(output_graph_def.SerializeToString())


ckpt_pb('shuffle.pb')
