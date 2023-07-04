import os
import time
import tensorflow as tf
import numpy as np
import datetime

from models import SampleAndAggregate, SAGEInfo, Node2VecModel
from minibatch import EdgeMinibatchIterator
from neigh_samplers import UniformNeighborSampler
from utils import preprocess_data

os.environ["CUDA_DEVICE_ORDER"]="PCI_BUS_ID"

# Set random seed
seed = 123
np.random.seed(seed)
tf.set_random_seed(seed)

# Settings
flags = tf.app.flags
FLAGS = flags.FLAGS

tf.app.flags.DEFINE_boolean('log_device_placement', False,
                            """Whether to log device placement.""")
#core params..
flags.DEFINE_string('model', 'graphsage_mean', 'model names. See README for possible values.')
flags.DEFINE_float('learning_rate', 0.01, 'initial learning rate.')
flags.DEFINE_string("model_size", "small", "Can be big or small; model specific def'ns")
flags.DEFINE_string('train_prefix', '/var/tmp/jasminegraph-localstore/jasminegraph-local_trained_model_store/6', 'name of the object file that stores the training data. must be specified.')
flags.DEFINE_string('train_attr_prefix', '/var/tmp/jasminegraph-localstore/6', 'name of the object file that stores the training attributes data. must be specified.')
flags.DEFINE_string('train_worker', '1' , 'specify the worker')
flags.DEFINE_integer('graph_id',6, 'specify the graphID')
flags.DEFINE_boolean('isLabel', True, 'whether dataset has labels')
flags.DEFINE_boolean('isFeatures', True, 'whether dataset has features')

# left to default values in main experiments 
flags.DEFINE_integer('epochs', 2, 'number of epochs to train.')
flags.DEFINE_float('dropout', 0.0, 'dropout rate (1 - keep probability).')
flags.DEFINE_float('weight_decay', 0.0, 'weight for l2 loss on embedding matrix.')
flags.DEFINE_integer('max_degree', 100, 'maximum node degree.')
flags.DEFINE_integer('samples_1', 25, 'number of samples in layer 1')
flags.DEFINE_integer('samples_2', 10, 'number of users samples in layer 2')
flags.DEFINE_integer('dim_1', 128, 'Size of output dim (final is 2x this, if using concat)')
flags.DEFINE_integer('dim_2', 128, 'Size of output dim (final is 2x this, if using concat)')
flags.DEFINE_boolean('random_context', False, 'Whether to use random context or direct edges')
flags.DEFINE_integer('neg_sample_size', 20, 'number of negative samples')
flags.DEFINE_integer('batch_size', 16, 'minibatch size.')
flags.DEFINE_integer('n2v_test_epochs', 1, 'Number of new SGD epochs for n2v.')
flags.DEFINE_integer('identity_dim', 0, 'Set to positive value to use identity embedding features of that dimension. Default 0.')

#logging, saving, validation settings etc.
flags.DEFINE_boolean('save_embeddings', True, 'whether to save embeddings for all nodes after training')
flags.DEFINE_string('base_log_dir', '/var/tmp/jasminegraph-localstore/', 'base directory for logging and saving embeddings')
flags.DEFINE_integer('validate_iter',5,"how often to run a validation minibatch.")
flags.DEFINE_integer('validate_batch_size', 4, "how many nodes per validation sample.")
flags.DEFINE_integer('gpu', 1, "which gpu to use.")
flags.DEFINE_integer('print_every', 50, "How often to print training info.")
flags.DEFINE_integer('max_total_steps', 10**10, "Maximum total number of iterations")

os.environ["CUDA_VISIBLE_DEVICES"]=str(FLAGS.gpu)

GPU_MEM_FRACTION = 0.8

def log_dir():

    log_dir = FLAGS.base_log_dir + "/jasminegraph-local_trained_model_store/"
    log_dir += "{graph_id:s}_model_{train_worker:s}".format(
        graph_id = FLAGS.train_prefix.split("/")[-1],
        train_worker = FLAGS.train_worker
    )
    if not os.path.exists(log_dir):
        os.makedirs(log_dir)
    return log_dir

def save_model(saver, sess, step):
    filename = log_dir() + "/" + str(step) + ".ckpt"
    if not os.path.exists(os.path.dirname(filename)):
        os.makedirs(os.path.dirname(filename))
    saver.save(sess, filename)

# Define model evaluation function
def evaluate(sess, model, minibatch_iter, size=None):
    t_test = time.time()
    feed_dict_val = minibatch_iter.val_feed_dict(size)
    outs_val = sess.run([model.loss, model.ranks, model.mrr], 
                        feed_dict=feed_dict_val)
    return outs_val[0], outs_val[1], outs_val[2], (time.time() - t_test)

# Define model evaluation function on test edges
def evaluate_test(sess, model, minibatch_iter, size=None):
    t_test = time.time()
    feed_dict_val = minibatch_iter.test_feed_dict(size)
    outs_val = sess.run([model.loss, model.ranks, model.mrr],
                        feed_dict=feed_dict_val)
    return outs_val[0], outs_val[1], outs_val[2], (time.time() - t_test)

def incremental_evaluate(sess, model, minibatch_iter, size):
    t_test = time.time()
    finished = False
    val_losses = []
    val_mrrs = []
    iter_num = 0
    while not finished:
        feed_dict_val, finished, _ = minibatch_iter.incremental_val_feed_dict(size, iter_num)
        iter_num += 1
        outs_val = sess.run([model.loss, model.ranks, model.mrr], 
                            feed_dict=feed_dict_val)
        val_losses.append(outs_val[0])
        val_mrrs.append(outs_val[2])
    return np.mean(val_losses), np.mean(val_mrrs), (time.time() - t_test)

def save_val_embeddings(sess, model, minibatch_iter, size, out_dir, mod=""):
    val_embeddings = []
    finished = False
    seen = set([])
    nodes = []
    iter_num = 0
    name = FLAGS.train_prefix.split("/")[-1] +"_model_"+FLAGS.train_worker
    while not finished:
        feed_dict_val, finished, edges = minibatch_iter.incremental_embed_feed_dict(size, iter_num)
        iter_num += 1
        outs_val = sess.run([model.loss, model.mrr, model.outputs1], 
                            feed_dict=feed_dict_val)
        #ONLY SAVE FOR embeds1 because of planetoid
        for i, edge in enumerate(edges):
            if not edge[0] in seen:
                val_embeddings.append(outs_val[-1][i,:])
                nodes.append(edge[0])
                seen.add(edge[0])
    if not os.path.exists(out_dir):
        os.makedirs(out_dir)
    val_embeddings = np.vstack(val_embeddings)
    np.save(out_dir + "/" + name + mod + ".npy",  val_embeddings)
    with open(out_dir +  "/" + name + mod + ".txt", "w") as fp:
        fp.write("\n".join(map(str,nodes)))

def save_central_val_embeddings(sess, model, minibatch_iter, size, out_dir, mod=""):
    val_embeddings = []
    finished = False
    seen = set([])
    nodes = []
    iter_num = 0
    name = FLAGS.train_prefix.split("/")[-1] +"_central_model_"+FLAGS.train_worker
    while not finished:
        feed_dict_val, finished, edges = minibatch_iter.incremental_central_embed_feed_dict(size, iter_num)
        iter_num += 1
        outs_val = sess.run([model.loss, model.mrr, model.outputs1],
                            feed_dict=feed_dict_val)
        #ONLY SAVE FOR embeds1 because of planetoid
        for i, edge in enumerate(edges):
            if not edge[0] in seen:
                val_embeddings.append(outs_val[-1][i,:])
                nodes.append(edge[0])
                seen.add(edge[0])
    if not os.path.exists(out_dir):
        os.makedirs(out_dir)
    if(len(val_embeddings)!=0):
        val_embeddings = np.vstack(val_embeddings)
        np.save(out_dir + "/" + name + mod + ".npy",  val_embeddings)
        with open(out_dir +  "/" + name + mod + ".txt", "w") as fp:
            fp.write("\n".join(map(str,nodes)))


def construct_placeholders():
    # Define placeholders
    placeholders = {
        'batch1' : tf.placeholder(tf.int32, shape=(None), name='batch1'),
        'batch2' : tf.placeholder(tf.int32, shape=(None), name='batch2'),
        # negative samples for all nodes in the batch
        'neg_samples': tf.placeholder(tf.int32, shape=(None,),
            name='neg_sample_size'),
        'dropout': tf.placeholder_with_default(0., shape=(), name='dropout'),
        'batch_size' : tf.placeholder(tf.int32, name='batch_size'),
    }
    return placeholders

def train(train_data, G_local,test_data=None):
    G = train_data[0]
    features = train_data[1]
    id_map = train_data[2]

    if not features is None:
        # pad with dummy zero vector
        features = np.vstack([features, np.zeros((features.shape[1],))])

    context_pairs = train_data[3] if FLAGS.random_context else None
    placeholders = construct_placeholders()
    minibatch = EdgeMinibatchIterator(G, G_local,
            id_map,
            placeholders, batch_size=FLAGS.batch_size,
            max_degree=FLAGS.max_degree, 
            num_neg_samples=FLAGS.neg_sample_size,
            context_pairs = context_pairs)
    edge_file = open(log_dir() + "/" +str(FLAGS.graph_id)+"_edge_detail_"+FLAGS.train_worker+".txt", "w")
    edge_file.write (str("train-edges"))
    edge_file.write (str(len(minibatch.train_edges)))
    edge_file.write (str("valid-edges"))
    edge_file.write (str(len(minibatch.val_edges)))
    edge_file.write (str("test-edges"))
    edge_file.write (str(len(minibatch.test_edges)))

    adj_info_ph = tf.placeholder(tf.int32, shape=minibatch.adj.shape)
    adj_info = tf.Variable(adj_info_ph, trainable=False, name="adj_info")

    if FLAGS.model == 'graphsage_mean':
        # Create model
        sampler = UniformNeighborSampler(adj_info)
        layer_infos = [SAGEInfo("node", sampler, FLAGS.samples_1, FLAGS.dim_1),
                            SAGEInfo("node", sampler, FLAGS.samples_2, FLAGS.dim_2)]

        model = SampleAndAggregate(placeholders, 
                                     features,
                                     adj_info,
                                     minibatch.deg,
                                     layer_infos=layer_infos, 
                                     model_size=FLAGS.model_size,
                                     identity_dim = FLAGS.identity_dim,
                                     logging=True)
    elif FLAGS.model == 'gcn':
        # Create model
        sampler = UniformNeighborSampler(adj_info)
        layer_infos = [SAGEInfo("node", sampler, FLAGS.samples_1, 2*FLAGS.dim_1),
                            SAGEInfo("node", sampler, FLAGS.samples_2, 2*FLAGS.dim_2)]

        model = SampleAndAggregate(placeholders, 
                                     features,
                                     adj_info,
                                     minibatch.deg,
                                     layer_infos=layer_infos, 
                                     aggregator_type="gcn",
                                     model_size=FLAGS.model_size,
                                     identity_dim = FLAGS.identity_dim,
                                     concat=False,
                                     logging=True)

    elif FLAGS.model == 'graphsage_seq':
        sampler = UniformNeighborSampler(adj_info)
        layer_infos = [SAGEInfo("node", sampler, FLAGS.samples_1, FLAGS.dim_1),
                            SAGEInfo("node", sampler, FLAGS.samples_2, FLAGS.dim_2)]

        model = SampleAndAggregate(placeholders, 
                                     features,
                                     adj_info,
                                     minibatch.deg,
                                     layer_infos=layer_infos, 
                                     identity_dim = FLAGS.identity_dim,
                                     aggregator_type="seq",
                                     model_size=FLAGS.model_size,
                                     logging=True)

    elif FLAGS.model == 'graphsage_maxpool':
        sampler = UniformNeighborSampler(adj_info)
        layer_infos = [SAGEInfo("node", sampler, FLAGS.samples_1, FLAGS.dim_1),
                            SAGEInfo("node", sampler, FLAGS.samples_2, FLAGS.dim_2)]

        model = SampleAndAggregate(placeholders, 
                                    features,
                                    adj_info,
                                    minibatch.deg,
                                     layer_infos=layer_infos, 
                                     aggregator_type="maxpool",
                                     model_size=FLAGS.model_size,
                                     identity_dim = FLAGS.identity_dim,
                                     logging=True)
    elif FLAGS.model == 'graphsage_meanpool':
        sampler = UniformNeighborSampler(adj_info)
        layer_infos = [SAGEInfo("node", sampler, FLAGS.samples_1, FLAGS.dim_1),
                            SAGEInfo("node", sampler, FLAGS.samples_2, FLAGS.dim_2)]

        model = SampleAndAggregate(placeholders, 
                                    features,
                                    adj_info,
                                    minibatch.deg,
                                     layer_infos=layer_infos, 
                                     aggregator_type="meanpool",
                                     model_size=FLAGS.model_size,
                                     identity_dim = FLAGS.identity_dim,
                                     logging=True)

    elif FLAGS.model == 'n2v':
        model = Node2VecModel(placeholders, features.shape[0],
                                       minibatch.deg,
                                       #2x because graphsage uses concat
                                       nodevec_dim=2*FLAGS.dim_1,
                                       lr=FLAGS.learning_rate)
    else:
        raise Exception('Error: model name unrecognized.')

    config = tf.ConfigProto(log_device_placement=FLAGS.log_device_placement)
    config.gpu_options.allow_growth = True
    config.allow_soft_placement = True
    
    # Initialize session
    sess = tf.Session(config=config)
    merged = tf.summary.merge_all()
    saver = tf.train.Saver()
     
    # Init variables
    sess.run(tf.global_variables_initializer(), feed_dict={adj_info_ph: minibatch.adj,model.features_placeholder: features})

    # Train model
    
    train_shadow_mrr = None
    shadow_mrr = None
    test_shadow_mrr = None

    total_steps = 0
    avg_time = 0.0
    epoch_val_costs = []

    train_adj_info = tf.assign(adj_info, minibatch.adj)
    val_adj_info = tf.assign(adj_info, minibatch.test_adj)
    for epoch in range(FLAGS.epochs): 
        minibatch.shuffle() 

        iter = 0
        print('Epoch: %04d' % (epoch + 1))
        epoch_val_costs.append(0)
        while not minibatch.end():
            # Construct feed dictionary
            feed_dict = minibatch.next_minibatch_feed_dict()
            feed_dict.update({placeholders['dropout']: FLAGS.dropout})

            t = time.time()
            # Training step
            outs = sess.run([merged, model.opt_op, model.loss, model.ranks, model.aff_all, 
                    model.mrr, model.outputs1], feed_dict=feed_dict)
            train_cost = outs[2]
            train_mrr = outs[5]
            if train_shadow_mrr is None:
                train_shadow_mrr = train_mrr#
            else:
                train_shadow_mrr -= (1-0.99) * (train_shadow_mrr - train_mrr)

            if iter % FLAGS.validate_iter == 0:
                # Validation
                sess.run(val_adj_info.op)
                val_cost, ranks, val_mrr, duration  = evaluate(sess, model, minibatch, size=FLAGS.validate_batch_size)
                sess.run(train_adj_info.op)
                epoch_val_costs[-1] += val_cost
            if shadow_mrr is None:
                shadow_mrr = val_mrr
            else:
                shadow_mrr -= (1-0.99) * (shadow_mrr - val_mrr)

            avg_time = (avg_time * total_steps + time.time() - t) / (total_steps + 1)

            if total_steps % FLAGS.print_every == 0:
                print("Epoch: ,%04d" % (epoch + 1),
                      "Iter:", '%04d' % iter,
                      "train_loss=", "{:.5f}".format(train_cost),
                      "train_mrr=", "{:.5f}".format(train_mrr), 
                      "train_mrr_ema=", "{:.5f}".format(train_shadow_mrr), # exponential moving average
                      "val_loss=", "{:.5f}".format(val_cost),
                      "val_mrr=", "{:.5f}".format(val_mrr), 
                      "val_mrr_ema=", "{:.5f}".format(shadow_mrr), # exponential moving average
                      "time=", "{:.5f}".format(avg_time))
                with open(log_dir() + "/"+str(FLAGS.graph_id)+"_validate_stats_"+FLAGS.train_worker+".txt", "a+") as fp:
                    fp.write("train_loss={:.5f} train_mrr={:.5f} train_mrr_ema={:.5f} val_loss={:.5f} val_mrr={:.5f} val_mrr_ema={:.5f} time={:.5f}".
                             format(train_cost, train_mrr, train_shadow_mrr,val_cost,val_mrr,shadow_mrr, avg_time))
                    fp.write("\n")


            iter += 1
            total_steps += 1

            if total_steps > FLAGS.max_total_steps:
                break

        if total_steps > FLAGS.max_total_steps:
                break

    train_end_file = open(log_dir() + "/" +str(FLAGS.graph_id)+"_train_end_time_"+FLAGS.train_worker+".txt", "w")
    trainendDT = datetime.datetime.now()
    print (str(trainendDT))
    train_end_file.write (str(trainendDT))

    print("Optimization Finished!")
    if FLAGS.save_embeddings:
        sess.run(val_adj_info.op)

        save_val_embeddings(sess, model, minibatch, FLAGS.validate_batch_size, log_dir())
        save_central_val_embeddings(sess, model, minibatch, FLAGS.validate_batch_size, log_dir())

        test_cost, test_ranks, test_mrr, test_duration  = evaluate_test(sess, model, minibatch, size=FLAGS.validate_batch_size)
        if test_shadow_mrr is None:
            test_shadow_mrr = test_mrr
        else:
            test_shadow_mrr -= (1-0.99) * (test_shadow_mrr - test_mrr)

        print("Full Test stats:",
              "test_loss=", "{:.5f}".format(test_cost),
              "test_mrr=", "{:.5f}".format(test_mrr),
              "test_mrr_ema=", "{:.5f}".format(test_shadow_mrr),
              "time=", "{:.5f}".format(test_duration))
        end_file = open(log_dir() + "/" +str(FLAGS.graph_id)+"_end_time_"+FLAGS.train_worker+".txt", "w")
        endDT = datetime.datetime.now()
        print (str(endDT))
        end_file.write (str(endDT))

        with open(log_dir() + "/" +str(FLAGS.graph_id)+"_test_stats_"+FLAGS.train_worker+".txt", "w") as fp:
            fp.write("test_loss={:.5f} test_mrr={:.5f} test_mrr_ema={:.5f} time={:.5f}".
                     format(test_cost, test_mrr, test_shadow_mrr, test_duration))

        test_edge_set = [e for e in G.edges() if G[e[0]][e[1]]['testing']]

        with open(log_dir() + "/"+str(FLAGS.graph_id)+"_TEST_EDGE_SET_"+FLAGS.train_worker+".txt", "a+") as fp:
            for e in test_edge_set:
                fp.write(str(e[0])+" "+str(e[1]))
                fp.write("\n")

        if FLAGS.model == "n2v":
            # stopping the gradient for the already trained nodes
            train_ids = tf.constant([[id_map[n]] for n in G.nodes_iter() if not G.node[n]['val'] and not G.node[n]['test']],
                    dtype=tf.int32)
            test_ids = tf.constant([[id_map[n]] for n in G.nodes_iter() if G.node[n]['val'] or G.node[n]['test']], 
                    dtype=tf.int32)
            update_nodes = tf.nn.embedding_lookup(model.context_embeds, tf.squeeze(test_ids))
            no_update_nodes = tf.nn.embedding_lookup(model.context_embeds,tf.squeeze(train_ids))
            update_nodes = tf.scatter_nd(test_ids, update_nodes, tf.shape(model.context_embeds))
            no_update_nodes = tf.stop_gradient(tf.scatter_nd(train_ids, no_update_nodes, tf.shape(model.context_embeds)))
            model.context_embeds = update_nodes + no_update_nodes
            sess.run(model.context_embeds)

            # run random walks
            from graphsage.utils import run_random_walks
            nodes = [n for n in G.nodes_iter() if G.node[n]["val"] or G.node[n]["test"]]
            start_time = time.time()
            pairs = run_random_walks(G, nodes, num_walks=50)
            walk_time = time.time() - start_time

            test_minibatch = EdgeMinibatchIterator(G, G_local,
                id_map,
                placeholders, batch_size=FLAGS.batch_size,
                max_degree=FLAGS.max_degree, 
                num_neg_samples=FLAGS.neg_sample_size,
                context_pairs = pairs,
                n2v_retrain=True,
                fixed_n2v=True)
            
            start_time = time.time()
            print("Doing test training for n2v.")
            test_steps = 0
            for epoch in range(FLAGS.n2v_test_epochs):
                test_minibatch.shuffle()
                while not test_minibatch.end():
                    feed_dict = test_minibatch.next_minibatch_feed_dict()
                    feed_dict.update({placeholders['dropout']: FLAGS.dropout})
                    outs = sess.run([model.opt_op, model.loss, model.ranks, model.aff_all, 
                        model.mrr, model.outputs1], feed_dict=feed_dict)
                    if test_steps % FLAGS.print_every == 0:
                        print("Iter:", '%04d' % test_steps, 
                              "train_loss=", "{:.5f}".format(outs[1]),
                              "train_mrr=", "{:.5f}".format(outs[-2]))
                    test_steps += 1
            train_time = time.time() - start_time
            save_val_embeddings(sess, model, minibatch, FLAGS.validate_batch_size, log_dir(), mod="-test")
            print("Total time: ", train_time+walk_time)
            print("Walk time: ", walk_time)
            print("Train time: ", train_time)

    

def main(argv=None):
    file = open(log_dir() + "/" +str(FLAGS.graph_id)+"_start_time_"+FLAGS.train_worker+".txt", "w")
    currentDT = datetime.datetime.now()
    print (str(currentDT))
    file.write (str(currentDT))


    print("Loading training data..")
    processed_data = preprocess_data(FLAGS.train_prefix, FLAGS.train_attr_prefix, FLAGS.train_worker,FLAGS.isLabel,FLAGS.isFeatures)
    train_data = processed_data[0:-1]
    G_local = processed_data[-1]
    print("Done loading training data..")
    file.write(str("Done loading training data.."))
    file.close()
    train(train_data,G_local)


if __name__ == '__main__':
    main()
