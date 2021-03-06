import os
import numpy as np
import pandas as pd
import tensorflow as tf
import cv2
import matplotlib.pyplot as plt
import matplotlib.image as mpimg
import heapq

from tqdm import tqdm
from dataset import *
from utils.words import *
from utils.coco.coco import *
from utils.coco.pycocoevalcap.eval import *

class ImageLoader(object):
    def __init__(self, mean_file):
        self.bgr = True 
        self.scale_shape = np.array([224, 224], np.int32)
        self.crop_shape = np.array([224, 224], np.int32)
        self.mean = np.load(mean_file).mean(1).mean(1)

    def load_img(self, img_file):    
        """ Load and preprocess an image. """  
        img = cv2.imread(img_file)

        if self.bgr:
            temp = img.swapaxes(0, 2)
            temp = temp[::-1]
            img = temp.swapaxes(0, 2)

        img = cv2.resize(img, (self.scale_shape[0], self.scale_shape[1]))
        offset = (self.scale_shape - self.crop_shape) / 2
        offset = offset.astype(np.int32)
        img = img[offset[0]:offset[0]+self.crop_shape[0], offset[1]:offset[1]+self.crop_shape[1], :]
        img = img - self.mean
        return img

    def load_imgs(self, img_files):
        """ Load and preprocess a list of images. """
        imgs = []
        for img_file in img_files:
            imgs.append(self.load_img(img_file))
        imgs = np.array(imgs, np.float32)
        return imgs

class Caption(object):
    def __init__(self, sentence, memory, output, score):
       self.sentence = sentence
       self.memory = memory
       self.output = output 
       self.score = score

    def __cmp__(self, other):
        assert isinstance(other, Caption)
        if self.score == other.score:
            return 0
        elif self.score < other.score:
            return -1
        else:
            return 1
  
class TopN(object):
    def __init__(self, n):
        self._n = n
        self._data = []

    def size(self):
        assert self._data is not None
        return len(self._data)

    def push(self, x):
        assert self._data is not None
        if len(self._data) < self._n:
            heapq.heappush(self._data, x)
        else:
            heapq.heappushpop(self._data, x)

    def extract(self, sort=False):
        assert self._data is not None
        data = self._data
        self._data = None
        if sort:
            data.sort(reverse=True)
        return data

    def reset(self):
        self._data = []

class BaseModel(object):
    def __init__(self, params, mode):
        self.params = params
        self.mode = mode
        self.save_dir = params.save_dir

        self.beam_size = params.beam_size       
        self.batch_size = params.batch_size

        self.cnn_model = params.cnn_model
        self.train_cnn = params.train_cnn

        self.dim_embed = params.dim_embed
        self.dim_hidden = params.dim_hidden
        self.dim_dec = params.dim_dec
        self.num_init_layers = params.num_init_layers
        self.init_dec_bias = params.init_dec_bias

        self.use_batch_norm = params.use_batch_norm      
        self.fc_keep_prob = params.fc_keep_prob
        self.lstm_keep_prob = params.lstm_keep_prob

        self.max_sent_len = params.max_sent_len
        self.word_table = WordTable(params.vocab_size, 
                                    params.dim_embed, 
                                    params.max_sent_len, 
                                    params.word_table_file)
        self.word_table.load()
        self.num_words = self.word_table.num_words

        self.class_balancing_factor = params.class_balancing_factor
        self.word_weight = np.exp(-np.array(self.word_table.word_freq)*self.class_balancing_factor)

        self.img_loader = ImageLoader(params.mean_file)
        self.img_shape = [224, 224, 3]

        self.global_step = tf.Variable(0, name = 'global_step', trainable = False)

        self.build()
  
    def build(self):
        raise NotImplementedError()

    def get_feed_dict(self, batch, is_train):
        raise NotImplementedError()

    def train(self, sess, train_coco, train_data):
        """ Train the model. """
        print("Training the model...")
        num_epochs = self.params.num_epochs

        train_writer = tf.summary.FileWriter("./", sess.graph)
        for epoch_no in tqdm(list(range(num_epochs)), desc='epoch'):
            for idx in tqdm(list(range(train_data.num_batches)), desc='batch'):

                batch = train_data.next_batch()
                feed_dict = self.get_feed_dict(batch, is_train=True)
                _, summary, global_step = sess.run([self.opt_op,                                                                     
                                                    self.summary, 
                                                    self.global_step], 
                                                    feed_dict=feed_dict)

                if (global_step + 1) % self.params.save_period == 0:
                    self.save(sess)
                
                train_writer.add_summary(summary, global_step)

            train_data.reset()

        self.save(sess)
        train_writer.close()

        print("Training complete.")

    def val(self, sess, val_coco, val_data, save_result_as_img=False):
        """ Validate the model. """
        if self.beam_size>1:
            self.val_beam_search(sess, val_coco, val_data, save_result_as_img)
        else:
            self.val_greedy(sess, val_coco, val_data, save_result_as_img)

    def val_beam_search(self, sess, val_coco, val_data, save_result_as_img=False):
        """ Evaluate the model using beam search. """
        print("Evaluating the model ...")
        results = []
        result_dir = self.params.val_result_dir

        # Generate the captions for the images
        cur_idx = 0
        for k in tqdm(list(range(val_data.num_batches))):
            batch = val_data.next_batch()
            caps = self.beam_search(sess, batch)

            fake_cnt = 0 if k<val_data.num_batches-1 else val_data.fake_count
            for l in range(val_data.batch_size-fake_cnt):            
                sent = caps[l][0].sentence
                score = caps[l][0].score
                sentence, _ = self.word_table.indices_to_sent(sent)
                results.append({'image_id': val_data.img_ids[cur_idx], 'caption': sentence}) 
                cur_idx += 1

                # Save the result in an image file
                if save_result_as_img:
                    img_file = batch[l]
                    img_name = os.path.splitext(img_file.split(os.sep)[-1])[0]
                    img = mpimg.imread(img_file)
                    plt.imshow(img)
                    plt.axis('off')
                    plt.title(sentence+'\n'+'(log prob='+str(score)+')')
                    plt.savefig(os.path.join(result_dir, img_name+'_result.jpg'))

        val_data.reset() 

        # Evaluate these captions
        val_res_coco = val_coco.loadRes2(results)
        scorer = COCOEvalCap(val_coco, val_res_coco)
        scorer.evaluate()
        print("Evaluation complete.")

    def val_greedy(self, sess, val_coco, val_data, save_result_as_img=False):
        """ Evaluate the model using the greedy strategy. """
        print("Evaluating the model ...")
        results = []
        result_dir = self.params.val_result_dir

        # Generate the captions for the images
        cur_idx = 0
        for k in tqdm(list(range(val_data.num_batches))):
            batch = val_data.next_batch()
            feed_dict = self.get_feed_dict(batch, is_train=False)
            result, logprob = sess.run([self.results, self.scores], feed_dict=feed_dict)

            fake_cnt = 0 if k<val_data.num_batches-1 else val_data.fake_count
            for l in range(val_data.batch_size-fake_cnt):            
                sentence, sent_len = self.word_table.indices_to_sent(result[l])
                score = np.sum(logprob[l, :sent_len])
                results.append({'image_id': val_data.img_ids[cur_idx], 'caption': sentence})
                cur_idx += 1 

                # Save the result in an image file
                if save_result_as_img:
                    img_file = batch[l]
                    img_name = os.path.splitext(img_file.split(os.sep)[-1])[0]
                    img = mpimg.imread(img_file)
                    plt.imshow(img)
                    plt.axis('off')
                    plt.title(sentence+'\n'+'(log prob='+str(score)+')')
                    plt.savefig(os.path.join(result_dir, img_name+'_result.jpg'))

        val_data.reset() 

        # Evaluate these captions
        val_res_coco = val_coco.loadRes2(results)
        scorer = COCOEvalCap(val_coco, val_res_coco)
        scorer.evaluate()
        print("Evaluation complete.")

    def test(self, sess, test_data, save_result_as_img=True):
        """Test the model."""
        if self.beam_size>1:
            self.test_beam_search(sess, test_data, save_result_as_img)
        else:
            self.test_greedy(sess, test_data, save_result_as_img)

    def test_beam_search(self, sess, test_data, save_result_as_img=True):
        """ Test the model using beam search. """
        print("Testing the model ...")
        result_file = self.params.test_result_file
        result_dir = self.params.test_result_dir
        captions = []
        scores = []

        # Generate the captions for the images
        for k in tqdm(list(range(test_data.num_batches))):
            batch = test_data.next_batch()
            caps = self.beam_search(sess, batch)

            fake_cnt = 0 if k<test_data.num_batches-1 else test_data.fake_count
            for l in range(test_data.batch_size-fake_cnt):  
                sent = caps[l][0].sentence
                score = caps[l][0].score
                sentence, _ = self.word_table.indices_to_sent(sent)
                captions.append(sentence)
                scores.append(score)

                # Save the result in an image file
                if save_result_as_img:
                    img_file = batch[l]
                    img_name = os.path.splitext(img_file.split(os.sep)[-1])[0]
                    img = mpimg.imread(img_file)
                    plt.imshow(img)
                    plt.axis('off')
                    plt.title(sentence+'\n'+'(log prob='+str(score)+')')
                    plt.savefig(os.path.join(result_dir, img_name+'_result.jpg'))        

        # Save the captions to a file
        results = pd.DataFrame({'image_files':test_data.img_files, 'caption':captions, 'score':scores})
        results.to_csv(result_file)
        print("Testing complete.")

    def test_greedy(self, sess, test_data, save_result_as_img=True):
        """ Test the model using the greedy strategy. """
        print("Testing the model ...")
        result_file = self.params.test_result_file
        result_dir = self.params.test_result_dir
        captions = []
        scores = []

        # Generate the captions for the images
        for k in tqdm(list(range(test_data.num_batches))):
            batch = test_data.next_batch()
            feed_dict = self.get_feed_dict(batch, is_train=False)
            result, logprob = sess.run([self.results, self.scores], feed_dict=feed_dict)

            fake_cnt = 0 if k<test_data.num_batches-1 else test_data.fake_count
            for l in range(test_data.batch_size-fake_cnt):            
                sentence, sent_len = self.word_table.indices_to_sent(result[l])
                score = np.sum(logprob[l, :sent_len])
                captions.append(sentence)
                scores.append(score)
        
                # Save the result in an image file
                if save_result_as_img:
                    img_file = batch[l]
                    img_name = os.path.splitext(img_file.split(os.sep)[-1])[0]
                    img = mpimg.imread(img_file)
                    plt.imshow(img)
                    plt.axis('off')
                    plt.title(sentence+'\n'+'(log prob='+str(score)+')')
                    plt.savefig(os.path.join(result_dir, img_name+'_result.jpg'))

        # Save the captions to a file
        results = pd.DataFrame({'image_files':test_data.img_files, 'caption':captions, 'score':scores})
        results.to_csv(result_file)
        print("Testing complete.")

    def beam_search(self, sess, img_files):
        """Use beam search to generate the captions for a batch of images."""
        # Feed in the images to get the contexts and the initial LSTM states
        imgs = self.img_loader.load_imgs(img_files)
        contexts, initial_memory, initial_output = sess.run([self.conv_feats, 
                                                             self.initial_memory, 
                                                             self.initial_output], 
                                                             feed_dict={self.imgs: imgs, 
                                                                        self.is_train: False})

        partial_captions = []
        complete_captions = []
        for k in range(self.batch_size):
            initial_beam = Caption(sentence=[], 
                                   memory=initial_memory[k], 
                                   output=initial_output[k], 
                                   score=0.0)
            partial_captions.append(TopN(self.beam_size))
            partial_captions[k].push(initial_beam)          
            complete_captions.append(TopN(self.beam_size))

        # Run beam search
        for idx in range(self.max_sent_len):

            partial_captions_lists = []
            for k in range(self.batch_size):            
                partial_captions_lists.append(partial_captions[k].extract())
                partial_captions[k].reset()

            num_steps = 1 if idx==0 else self.beam_size
            for b in range(num_steps):
                if idx==0:
                    last_word = np.zeros((self.batch_size), np.int32)
                else:
                    last_word = np.array([pcl[b].sentence[-1] for pcl in partial_captions_lists], np.int32)

                last_memory = np.array([pcl[b].memory for pcl in partial_captions_lists], np.float32)
                last_output = np.array([pcl[b].output for pcl in partial_captions_lists], np.float32)

                memory, output, logprobs = sess.run([self.memory, self.output, self.logprobs], 
                                                    feed_dict={self.contexts: contexts,
                                                               self.last_word: last_word,
                                                               self.last_memory: last_memory,
                                                               self.last_output: last_output, 
                                                               self.initial_step: idx==0,
                                                               self.is_train: False})

                # Find the beam_size most probable next words
                for  k in range(self.batch_size):
                    partial_caption = partial_captions_lists[k][b]
                    words_and_logprobs = list(enumerate(logprobs[k]))
                    words_and_logprobs.sort(key=lambda x: -x[1])
                    words_and_logprobs = words_and_logprobs[0:self.beam_size]

                    # Append each of these words to the current partial caption
                    for w, lp in words_and_logprobs:
                        sentence = partial_caption.sentence + [w]
                        score = partial_caption.score + lp
                        beam = Caption(sentence, memory[k], output[k], score)
                        if self.word_table.idx2word[w] == ".":
                            complete_captions[k].push(beam)
                        else:
                            partial_captions[k].push(beam)

        results = []
        for k in range(self.batch_size):
            if complete_captions[k].size()==0:
                complete_captions[k] = partial_captions[k]
            results.append(complete_captions[k].extract(sort=True))

        return results
 
    def save(self, sess):
        """ Save the model. """
        data = {v.name: v.eval() for v in tf.global_variables()}
        save_path = os.path.join(self.save_dir, str(self.global_step.eval()))  

        print((" Saving the model to %s..." % (save_path+".npy")))

        np.save(save_path, data)
        info_path = os.path.join(self.save_dir, "info")  
        info_file = open(info_path, "wb")
        info_file.write(str(self.global_step.eval()))
        info_file.close()          

        print("Model saved.")

    def load(self, sess):
        """ Load the model. """
        if self.params.model_file is not None:
            save_path = self.params.model_file        
        else:
            info_path = os.path.join(self.save_dir, "info")  
            info_file = open(info_path, "rb")
            global_step = info_file.read()
            info_file.close() 
            save_path = os.path.join(self.save_dir, global_step+".npy")  
 
        print("Loading the model from %s..." % save_path)
   
        data_dict = np.load(save_path).item()
        for v in tf.global_variables(): 
            if v.name in data_dict.keys():
                sess.run(v.assign(data_dict[v.name]))

        print("Model loaded.")
   
    def load_cnn(self, data_path, session, ignore_missing=True):
        """ Load a pretrained CNN model. """
        print("Loading CNN model from %s..." %data_path)
        data_dict = np.load(data_path).item()
        count = 0
        with tf.variable_scope("CNN", reuse=True):
            for op_name in data_dict:
                with tf.variable_scope(op_name, reuse=True):
                    for param_name, data in data_dict[op_name].iteritems():
                        try:
                            var = tf.get_variable(param_name)
                            session.run(var.assign(data))
                            count += 1
                        except ValueError:
                            if not ignore_missing:
                                raise
        print("%d tensors loaded. " %count)

