import parameterwebsocketclient
import pyspark
from operator import add
import websocket
import json
import threading
import tornado.web
import tornado.ioloop
import tornado.websocket
#import mnistcnn
import os
import mnistdnn
import higgsdnn
import moleculardnn
import tensorflow as tf
#import pickle
import time
from sacred import Experiment
from sacred.observers import MongoObserver
import random
import cStringIO
import numpy as np 
from guppy import hpy
hp = hpy()
from memory_profiler import profile
import sys

directory = "/Users/christophersmith/code/adatao/tensorspark/data/"

ex = Experiment('tensorspark')
ex.observers.append(MongoObserver.create(db_name='tensorspark_experiments'))

batch_sz = 100
model_keyword = 'mnist'

if model_keyword == 'mnist':
    training_rdd_filename = '%smnist_train.csv' % directory
    test_filename = '%smnist_test.csv' % directory  
    local_test_path = '%smnist_test.csv' % directory
    model = mnistdnn.MnistDNN(batch_sz)
elif model_keyword == 'higgs':
    training_rdd_filename = '%shiggs_train_all.csv' % directory
    test_filename = '%shiggs_test_all.csv' % directory         
    local_test_path = '/home/ubuntu/higgs_test_all.csv'
    model = higgsdnn.HiggsDNN(batch_sz)
elif model_keyword == 'molecular':
#    batch_sz = 1
    training_rdd_filename = '%smolecular_train_all.csv' % directory
    test_filename = '%smolecular_test_all.csv' % directory       
    local_test_path = '/home/ubuntu/molecular_test_all.csv'
    model = moleculardnn.MolecularDNN(batch_sz)
else:
    print("KEYWORD HAS TO BE 'mnist', 'higgs' or 'molecular'")
    sys.exit(1)

t = int(time.time())
error_rates_path = '/Users/christophersmith/code/adatao/tensorspark/parameter_server/error_rates_%s_%d.txt' % (model_keyword, t)
conf = pyspark.SparkConf()
#conf.setMaster('yarn')
#conf.set('spark.driver.memory', '14g')
#conf.set('spark.executor.memory', '8g')
#conf.set('spark.driver.maxResultSize', '14g')
#conf.set('spark.yarn.am.memory', '10g')
#conf.set('yarn.nodemanager.resource.memory-mb', '2000')
sc = pyspark.SparkContext(conf=conf)
websocket_port = random.randint(30000, 60000)
print 'websocket_port %d' % websocket_port
class ParameterServerWebsocketHandler(tornado.websocket.WebSocketHandler):

	def __init__(self, *args, **kwargs):
		self.server = kwargs.pop('server')
		self.model = self.server.model
		with self.model.session.graph.as_default():
			self.saver = tf.train.Saver()
		self.lock = threading.Lock()		
		super(ParameterServerWebsocketHandler,self).__init__(*args, **kwargs)

	def open(self):
		pass
#		print "New client connected"

	def on_close(self):
		pass	
#	print "Client disconnected"
#	@profile(stream=sys.stdout)
	def on_message(self, message):

		message = json.loads(message)
	#	print 'received message %s' % message['type']
		if message['type'] == 'client_requests_parameters':
			self.lock.acquire()
			parameters = self.model.get_parameters()
			self.lock.release()
			serialized = self.model.serialize(parameters)
			self.write_message(serialized)
			del serialized
			del parameters
		elif message['type'] == 'client_gives_gradient':
			# deserialize!
			gradient = self.model.deserialize(message['gradient'])
			
			self.lock.acquire()
			self.model.apply(gradient)
			error_rate = self.model.test(self.server.test_labels, self.server.test_features)      
			print error_rate
			t = time.time()
			with open(error_rates_path, 'a') as f:
        			f.write('%f , %f\n' % (t, error_rate))                  
			self.lock.release()
			del gradient
			del error_rate
		elif message['type'] == 'save_model':
			if not os.path.exists('./models'):
				os.makedirs('./models')
			self.saver.save(self.model.session, './models/parameter_server_model', global_step=int(time.time()))
		elif message['type'] == 'restore_model':
			model_path = message['model_path']
			model_path = message['model_path']
			self.saver.restore(self.model.session, model_path)
		else:
			print 'Unknown message type %s' % message['type']
		del message



class ParameterServer(threading.Thread):

	def __init__(self, model, warmup_data=None, test_data=None):
		threading.Thread.__init__(self)
		self.model = model
		test_labels, test_features = model.process_data(test_data)
		self.test_features = test_features
		self.test_labels = test_labels
		self.warmup(warmup_data)
		self.application = tornado.web.Application([(r"/", ParameterServerWebsocketHandler, {'server':self})])
	
	def warmup(self, data=None):
		if data is not None:
			self.model.train_warmup(partition=data, error_rates_filename=error_rates_path)

	def run(self):
		self.application.listen(websocket_port)
   		tornado.ioloop.IOLoop.current().start()


def train_partition(partition):
	return parameterwebsocketclient.TensorSparkWorker(model_keyword, batch_sz, websocket_port).train_partition(partition)

def test_partition(partition):
	return parameterwebsocketclient.TensorSparkWorker(model_keyword, batch_sz, websocket_port).test_partition(partition)

# you can find the mnist csv files here http://pjreddie.com/projects/mnist-in-csv/
@ex.capture
def train_epochs(num_epochs, training_rdd, num_partitions):
	for i in range(num_epochs):
		print 'training epoch %d' % i
		training_rdd = training_rdd.repartition(num_partitions)
		mapped_training = training_rdd.mapPartitions(train_partition)
		mapped_training.collect()
		#training_rdd.repartition(training_rdd.getNumPartitions())


def test_all():
	testing_rdd = sc.textFile(test_filename)
	#testing_rdd = sc.textFile('%shiggs_test_all.csv' % directory)
	#testing_rdd = sc.textFile('%smolecular_test_all.csv' % directory)
	mapped_testing = testing_rdd.mapPartitions(test_partition)
	return mapped_testing.reduce(add)/mapped_testing.getNumPartitions()


#	return mapped_testing.reduce(add)/mapped_testing.getNumPartitions()

def save_model():
	websock = websocket.create_connection('ws://localhost:%d' % websocket_port)
	message = {'type': 'save_model'}
	websock.send(json.dumps(message))


def start_parameter_server(model, warmup_data,test_data):
	parameter_server = ParameterServer(model=model, warmup_data=warmup_data, test_data=test_data)
	parameter_server.start()
	return parameter_server


@ex.config
def configure_experiment():
	warmup_iterations = 2000
	num_epochs = 5
	batch_size = batch_sz
	num_partitions = 96


@ex.capture
@ex.automain
def main(warmup_iterations, num_epochs):
	try:
		training_rdd = sc.textFile(training_rdd_filename)                                                                                     
                print(training_rdd.getNumPartitions())
                time.sleep(5)

		warmup_data = training_rdd.take(warmup_iterations)
			
		with open(local_test_path) as test_file:
        		test_data_lines = test_file.readlines()
           
		with open(error_rates_path, 'w') as f:
        		f.write('')            
		test_data = test_data_lines[0:1000]  

		parameter_server = start_parameter_server(model=model, warmup_data=warmup_data, test_data=test_data)
		#raw_input('Press enter to continue\n')

		#training_rdd = training_rdd.subtract(sc.parallelize(warmup_data))
		train_epochs(num_epochs, training_rdd)
#		print 'Done training'
		save_model()
#		print 'Testing now'
		test_results = test_all()
#		print test_results
#		sc.show_profiles()
		t = time.time()
		with open(error_rates_path, 'a') as f:
			f.write('%f , %f\ndone' % (t, test_results))
		print test_results
		print 'done'
		return test_results
	finally:
		tornado.ioloop.IOLoop.current().stop()


