import os
import json
import math
import time
import argparse
import logging
import random
import numpy as np

import pickle
from tqdm import tqdm
import spacy

import torch
import torch.nn as nn
import torch.optim as optim
import torch.nn.functional as F
from torchtext.data.metrics import bleu_score

import Transformer
from helper import *
from Multi30kDataLoader import Multi30kDataLoader

import nltk

torch.cuda.empty_cache()
device = torch.device('cuda' if torch.cuda.is_available() else 'cpu')


# set seed
torch.manual_seed(1)
random.seed(1)
np.random.seed(1)


class NoamOpt:
    "Optim wrapper that implements rate."

    def __init__(self, model_size, factor, warmup, optimizer):
        self.optimizer = optimizer
        self._step = 0
        self.warmup = warmup
        self.factor = factor
        self.model_size = model_size
        self._rate = 0

    def step(self):
        "Update parameters and rate"
        self._step += 1
        rate = self.rate()
        for p in self.optimizer.param_groups:
            p['lr'] = rate
        self._rate = rate
        self.optimizer.step()

    def rate(self, step=None):
        "Implement `lrate` above"
        if step is None:
            step = self._step
        return self.factor * \
            (self.model_size ** (-0.5) *
             min(step ** (-0.5), step * self.warmup ** (-1.5)))


def epoch_time(start_time, end_time):
    elapsed_time = end_time - start_time
    elapsed_mins = int(elapsed_time / 60)
    elapsed_secs = int(elapsed_time - (elapsed_mins * 60))
    return elapsed_mins, elapsed_secs

def beam_translate(tokens, src_field, trg_field, model, k, max_len=2000, logging=False):
    model.eval() # change into the evaluation mode

    # append <sos> token at the beginning and <eos> token at the end
    tokens = [special_symbols[2]] + tokens + [special_symbols[3]]
    if logging:
        print(f"full source token: {tokens}")

    src_indexes = [src_field[token] for token in tokens]
    if logging:
        print(f"source sentence index: {src_indexes}")


    src_tensor = torch.LongTensor(src_indexes).unsqueeze(0).to(device)
    src_mask = Transformer.get_padding_mask(src_tensor, PAD_IDX).to(device)
    memory = model.encode(src_tensor, src_mask).to(device)

    memory  = memory.expand(k,memory.shape[1],memory.shape[2])
    src_mask = src_mask.expand(k,src_mask.shape[1],src_mask.shape[2])

    k_prev_words = torch.full((k, 1), BOS_IDX, dtype=torch.long).to(device) # (k, 1)

    seqs = k_prev_words.to(device) #(k, 1)
    # prepare for beam search
    top_k_scores = torch.zeros(k, 1).to(device)
    complete_seqs = list()
    complete_seqs_scores = list()
    step = 1

    while True:
        tgt_mask = Transformer.get_padding_mask(
            seqs, PAD_IDX).to(device)
        peek_mask = Transformer.get_peek_mask(seqs).to(device)
        out = model.decode(seqs, memory[:k],
                           src_mask[:k], tgt_mask, peek_mask)
        outputs = model.generator(out)
        next_token_logits = outputs[:,-1,:] # (k, vocab_size)
        next_token_logits = torch.log_softmax(next_token_logits, dim=-1)
        next_token_logits += top_k_scores
        if step == 1:
            # the first step only has k BOS
            top_k_scores, top_k_words = next_token_logits[0].topk(k, dim=0, largest=True, sorted=True)
        else:
            top_k_scores, top_k_words = next_token_logits.view(-1).topk(k, 0, True, True)
        prev_word_inds = torch.div(top_k_words,len(trg_field.vocab),rounding_mode = 'floor')  # (k)  beam_id
        next_word_inds = top_k_words % len(trg_field.vocab)  # (k) token_id

        # seqs: (k, step) ==> (k, step+1)
        seqs = torch.cat([seqs[prev_word_inds], next_word_inds.unsqueeze(1)], dim=1)

        # check whether their is EOS token
        incomplete_inds = [ind for ind, next_word in enumerate(next_word_inds) if next_word != EOS_IDX]
        # output the beam id for those complete sentence
        complete_inds = list(set(range(len(next_word_inds))) - set(incomplete_inds))

        # if there are some complete sentences
        if len(complete_inds) > 0:
            complete_seqs.extend(seqs[complete_inds].tolist()) # add complete sentences to the list
            complete_seqs_scores.extend(top_k_scores[complete_inds]) # add the probability to the list
        # update k
        k -= len(complete_inds)

        if k == 0: # find all the complete sentences
            break

        # update the incomplete sentences
        seqs = seqs[incomplete_inds]
        top_k_scores = top_k_scores[incomplete_inds].unsqueeze(1)   #(s, 1) s < k

        if step > max_len:
            break
        step += 1

    i = complete_seqs_scores.index(max(complete_seqs_scores))
    seq = complete_seqs[i]
    trg_tokens = [trg_field.get_itos()[i] for i in seq]
    return trg_tokens[1:]

def translate_sentence(tokens, src_field, trg_field, model, max_len=2000, logging=True):
    model.eval()  # change into the evaluation mode

    # append <sos> token at the beginning and <eos> token at the end
    # special_symbols[2]] + sentence_tok + [special_symbols[3]
    tokens = [special_symbols[2]] + tokens + [special_symbols[3]]
    if logging:
        print(f"full source token: {tokens}")

    src_indexes = [src_field[token] for token in tokens]
    if logging:
        print(f"source sentence index: {src_indexes}")


    src_tensor = torch.LongTensor(src_indexes).unsqueeze(0).to(device)
    src_padding_mask = Transformer.get_padding_mask(src_tensor, PAD_IDX).to(device)

    with torch.no_grad():
        memory = model.encode(src_tensor, src_padding_mask)

    trg_indexes = [BOS_IDX]

    ys = torch.ones(1, 1).fill_(BOS_IDX).type(torch.long).to(device)

    with torch.no_grad():
        for i in range(max_len-1):
            # generate our output
            memory = memory.to(device)
            trg_padding_mask = Transformer.get_padding_mask(
                ys, PAD_IDX).to(device)
            peek_mask = Transformer.get_peek_mask(ys).to(device)
            # print("ys shape: ", ys.shape)
            # print("mem shape: ", memory.shape)
            out = model.decode(ys, memory,
                               src_padding_mask, trg_padding_mask, peek_mask)
            # out = out.transpose(0, 1)
            # print("out shape: ", out.shape)
            # print("generator input shape: ", out[:, -1].shape)
            prob = model.generator(out[:, -1])
            # print("prob shape: ", prob.shape)
            _, next_word = torch.max(prob, dim=1)
            next_word = next_word.item()

            # trg_indexes.append(pred_token)  # add to output statement
            ys = torch.cat([ys,
                            torch.ones(1, 1).type_as(src_tensor.data).fill_(next_word)], dim=1)
            # The moment you meet <eos>, it ends
            if next_word == EOS_IDX:
                break

    #  Convert each output word index to an actual word
    trg_tokens = [trg_field.get_itos()[i] for i in ys.squeeze()]

    # Returns the output statementz excluding the first <sos>
    return trg_tokens[1:]


def show_bleu(iterator, src_field, trg_field, model, device, max_len=50, beam=False):
    index = 0
    trgs = []
    pred_trgs = []
    src_tokenizer, trg_tokenizer = iterator.get_tokenizer()

    for datum in iterator.dataset[:]:
        src, trg = datum
        trg_tok = trg_tokenizer(trg.lower())
        src_tok = src_tokenizer(src.lower())


        if beam:
            k = 1 # beam width
            pred_trg = beam_translate(src_tok, src_field, trg_field, model, k, max_len = len(src_tok)+50, logging=False)
        else:
            pred_trg = translate_sentence(src_tok, src_field, trg_field, model, max_len, logging=False)

        # Remove the last <eos> token
        pred_trg = pred_trg[:-1]

        pred_trgs.append(pred_trg)
        trgs.append([trg_tok])

        index += 1
        if (index + 1) % 100 == 0:
            print(f"[{index + 1}/{len(iterator.dataset)}]")
            print(f"pred: {pred_trg}")
            print(f"trg: {trg_tok}")


    bleu = nltk.translate.bleu_score.corpus_bleu(trgs, pred_trgs, weights=[0.25, 0.25, 0.25, 0.25])

    individual_bleu1_score = nltk.translate.bleu_score.corpus_bleu(
        trgs, pred_trgs, weights=[1, 0, 0, 0])
    individual_bleu2_score = nltk.translate.bleu_score.corpus_bleu(
        trgs, pred_trgs, weights=[0, 1, 0, 0])
    individual_bleu3_score = nltk.translate.bleu_score.corpus_bleu(
        trgs, pred_trgs, weights=[0, 0, 1, 0])
    individual_bleu4_score = nltk.translate.bleu_score.corpus_bleu(
        trgs, pred_trgs, weights=[0, 0, 0, 1])

    logging.info(f'BLEU Score = {bleu*100:.2f}'
    + f'| BLEU-1 = {individual_bleu1_score*100:.2f} | BLEU-2 = {individual_bleu2_score*100:.2f}'
    + f'| BLEU-3 = {individual_bleu3_score*100:.2f} | BLEU-4 = {individual_bleu4_score*100:.2f}')
    return bleu, individual_bleu1_score, individual_bleu2_score, individual_bleu3_score, individual_bleu4_score


def cal_loss(pred, gold, trg_pad_idx, smoothing=False):
    gold = gold.contiguous().view(-1)

    if smoothing:
        eps = 0.1
        n_class = pred.size(-1)
        one_hot = torch.zeros_like(pred).scatter(1, gold.view(-1, 1), 1)
        one_hot = one_hot * (1 - eps) + (1 - one_hot) * eps / (n_class - 1)
        log_prb = F.log_softmax(pred, dim=1)

        non_pad_mask = gold.ne(trg_pad_idx)
        loss = -(one_hot * log_prb).sum(dim=1)
        loss = loss.masked_select(non_pad_mask).mean()
    else:
        loss = F.cross_entropy(
            pred, gold, ignore_index=trg_pad_idx, reduction='mean')
    return loss


def train(model, iterator, optimizer, criterion, epoch_num, clip=1, log_iter=100):
    model.train()  # set as train model
    epoch_loss = 0
    n_batch = 0
    # iterate through whole data set
    # [TODO]: Add progress bar
    for i, batch in enumerate(iterator):
        n_batch += 1
        src, trg = batch
        src = src.to(device)
        trg = trg.to(device)

        optimizer.optimizer.zero_grad()

        trg_in = trg[:, :-1].clone().to(device)
        trg_y = trg[:, 1:].clone().to(device)
        # exclude the end of the sentence
        # start with the beginning of the sentence
        src_padding_mask = Transformer.get_padding_mask(
            src, PAD_IDX).to(device)
        trg_padding_mask = Transformer.get_padding_mask(
            trg_in, PAD_IDX).to(device)
        peek_mask = Transformer.get_peek_mask(trg_in).to(device)
        output = model(src, trg_in, src_padding_mask,
                       trg_padding_mask, peek_mask)
        # output: [batch size, trg_len - 1, output_dim]
        # trg: [batch size, trg_len]
        output_dim = output.shape[-1]
        output = output.contiguous().view(-1, output_dim)
        # Exclude index 0 (<sos>) of output word
        trg_y = trg_y.contiguous().view(-1)
        loss = cal_loss(output, trg_y, PAD_IDX, True)

        loss.backward()

        # gradient clipping
        torch.nn.utils.clip_grad_norm_(model.parameters(), clip)
        # update the parameters
        optimizer.step()
        # calculate total loss
        epoch_loss += loss.item()
        if (i+1) % log_iter == 0:
            train_ppl = math.exp(min(loss, 100))
            logging.info(
                f"epoch {epoch_num} | bacth: {i+1}/{len(iterator)} | train_loss: {loss:.3f} | train_ppl: {train_ppl}")

    return epoch_loss / len(iterator)


def evaluate(model, iterator, criterion):
    model.eval()  # change to evaluation mode
    epoch_loss = 0

    with torch.no_grad():
        # Checking the entire evaluation data
        for i, batch in enumerate(iterator):
            src, trg = batch
            src = src.to(device)
            trg = trg.to(device)

            trg_in = trg[:, :-1].clone().to(device)
            trg_y = trg[:, 1:].clone().to(device)

            # exclude the end of the sentence
            # start with the beginning of the sentence
            src_padding_mask = Transformer.get_padding_mask(
                src, PAD_IDX).to(device)
            trg_padding_mask = Transformer.get_padding_mask(
                trg_in, PAD_IDX).to(device)
            peek_mask = Transformer.get_peek_mask(trg_in).to(device)
            output = model(src, trg_in, src_padding_mask,
                           trg_padding_mask, peek_mask)

            output_dim = output.shape[-1]

            output = output.contiguous().view(-1, output_dim)
            # Exclude index 0 (<sos>) of output word
            trg_y = trg_y.contiguous().view(-1)

            # output: [batch size * trg_len - 1, output_dim]
            # trg: [batch size * trg len - 1]

            # calculate the loss
            loss = cal_loss(output, trg_y, PAD_IDX, False)

            # calculate total loss
            epoch_loss += loss.item()

    return epoch_loss / len(iterator)


def train_model(model, train_iterator, valid_iterator, optimizer, n_epochs, clip, args):

    # save the losses
    log = []
    # ignore the padding index
    criterion = nn.CrossEntropyLoss(ignore_index=PAD_IDX, label_smoothing=0.1)
    best_valid_loss = float('inf')
    src_vocab = train_iterator.vocab['src']
    trg_vocab = train_iterator.vocab['trg']

    for epoch in range(n_epochs):
        metrics = {}
        start_time = time.time()  # record the start time

        train_loss = train(model, train_iterator, optimizer, criterion, epoch, clip)
        train_ppl = math.exp(min(train_loss, 100))

        valid_loss = evaluate(model, valid_iterator, criterion)
        valid_ppl = math.exp(min(valid_loss, 100))

        end_time = time.time()  # record the end time
        epoch_mins, epoch_secs = epoch_time(start_time, end_time)

        logging.info(f'Epoch: {epoch:02} | Time: {epoch_mins}m {epoch_secs}s |'
                     + f' valid_loss: {valid_loss:.3f} valid_ppl: {valid_ppl} |')

        checkpoint = {'epoch': epoch, 'model': model.state_dict()}

        save_path = os.path.join(
            args.log_dir, 'transformer_epoch_{}.ckpt'.format(epoch))

        torch.save(checkpoint, save_path)
        if valid_loss < best_valid_loss:
            best_valid_loss = valid_loss
            model_name = os.path.join(args.log_dir, 'transformer_best.ckpt')
            torch.save(checkpoint, model_name)
            logging.info(f"Updated best model to: {model_name}")

        metrics['train_loss'] = train_loss
        metrics['train_PPL'] = train_ppl

        metrics['valid_loss'] = valid_loss
        metrics['valid_PPL'] = valid_ppl

        log += [metrics]
        # [TODO]: change saving structure
        # dump into json file
        log_name = os.path.join(args.log_dir, 'train.log.json')
        with open(log_name, 'w') as f:
            json.dump(log, f)


def eval_model(model, data_iterator, args):
    log = []
    criterion = nn.CrossEntropyLoss(ignore_index=PAD_IDX)
    src_vocab = data_iterator.vocab['src']
    trg_vocab = data_iterator.vocab['trg']
    loss = evaluate(model, data_iterator, criterion)
    ppl = math.exp(min(loss, 100))
    bleu, bleu1, bleu2, bleu3, bleu4 = show_bleu(
        data_iterator, src_vocab, trg_vocab, model, device)
    metric = {}
    metric['loss'] = loss
    metric['ppl'] = ppl
    metric['bleu'] = bleu
    metric['bleu1'] = bleu1
    metric['bleu2'] = bleu2
    metric['bleu3'] = bleu3
    metric['bleu4'] = bleu4
    log += [metric]
    log_name = os.path.join(args.log_dir, 'evaluate.log.json')
    with open(log_name, 'w') as f:
        json.dump(log, f)
    return


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--log_dir", type=str, required=True)
    parser.add_argument("--use_bpe", action='store_true',
                        help="True if data is in BPE")
    parser.add_argument("--batch_size", type=int, default=16)
    parser.add_argument("--num_workers", type=int, default=2)
    parser.add_argument("--eval_only", action='store_true')
    parser.add_argument("--ckpt_path", type=str)
    args = parser.parse_args()

    if not os.path.exists(args.log_dir):
        os.mkdir(args.log_dir)

    logging_dir = os.path.join(args.log_dir, "debug.log")
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        handlers=[
            logging.FileHandler(logging_dir),
            logging.StreamHandler()
        ]
    )

    device_count =  torch.cuda.device_count()
    
    # config
    dim_model = 512
    dim_hidden = 2048
    num_layers = 6
    num_heads = 8
    n_epochs = 10
    warmup = 4000
    clip = 1

    def count_parameters(model):
        return sum(p.numel() for p in model.parameters() if p.requires_grad)

    def initialize_weights(m):
        if hasattr(m, 'weight') and m.weight.dim() > 1:
            nn.init.xavier_uniform_(m.weight.data, 0.5)
        if hasattr(m, 'bias') and m.bias is not None:
            nn.init.constant_(m.bias, 0)

    if not args.eval_only:
        logging.info("Running train")
        logging.info(f"Loading data...")
        train_iterator = Multi30kDataLoader(
            'en', 'de', 'train', batch_size=args.batch_size, num_workers=args.num_workers, use_bpe=args.use_bpe)
        valid_iterator = Multi30kDataLoader(
            'en', 'de', 'valid', batch_size=args.batch_size, num_workers=args.num_workers, use_bpe=args.use_bpe)
        test_iterator = Multi30kDataLoader(
            'en', 'de', 'test', batch_size=args.batch_size, num_workers=args.num_workers, use_bpe=args.use_bpe)
        logging.info(f"training dataset: {train_iterator.data_size} \n"
                     + f"validation dataset: {valid_iterator.data_size} \n"
                     + f"Test dataset: {test_iterator.data_size} \n")

        src_vocab_dim = len(train_iterator.vocab['src'])
        trg_vocab_dim = len(train_iterator.vocab['trg'])

        logging.info(f"src_vocab: {src_vocab_dim} \n" +
                     f"trg_vocab: {trg_vocab_dim} \n")
        logging.info(f"Running on: {device}")
        model = Transformer.TransformerModel(
            dim_model, dim_hidden, src_vocab_dim, trg_vocab_dim, N=num_layers, h=num_heads).to(device)

        model.to(device)

        logging.info(model)
        logging.info(
            f'Created model: The model has {count_parameters(model)} trainable parameters')
        model.apply(initialize_weights)
        # Adam optimizer with lr scheduling
        optimizer = NoamOpt(dim_model, 1, warmup,
                            torch.optim.Adam(model.parameters(), betas=(0.9, 0.98), eps=1e-9))
        # optimizer = torch.optim.Adam(model.parameters(), lr=0.0001,betas=(0.9, 0.98), eps=1e-9)
        train_model(model, train_iterator, valid_iterator,
                    optimizer, n_epochs, clip, args)

        logging.info("Running eval")
        eval_model(model, test_iterator, args)
    else:
        logging.info(f"Eval only")
        logging.info(f"Loading data...")
        test_iterator = Multi30kDataLoader(
            'en', 'de', 'test', batch_size=args.batch_size, num_workers=args.num_workers, use_bpe=args.use_bpe)
        logging.info(f"Test dataset: {len(test_iterator)} \n")
        src_vocab_dim = len(test_iterator.vocab['src'])
        trg_vocab_dim = len(test_iterator.vocab['trg'])
        logging.info(f"src_vocab: {src_vocab_dim} \n" +
                     f"trg_vocab: {trg_vocab_dim} \n")
        logging.info(f"Running on: {device}")
        model = Transformer.TransformerModel(
            dim_model, dim_hidden, src_vocab_dim, trg_vocab_dim, N=num_layers, h=num_heads)


        model.to(device)
        # args.ckpt_path = os.path.join(args.log_dir, 'transformer_best.ckpt') 
        if not os.path.exists(args.ckpt_path):
            logging.error(f"Ckpt not found: {args.ckpt_path}")
            return
        ckpt = torch.load(args.ckpt_path)
        model.load_state_dict(ckpt['model'])
        model.to(device)
        logging.info(model)
        logging.info(
            f'Loaded model from {args.ckpt_path} with {count_parameters(model)} parameters')

        logging.info("Running eval")
        eval_model(model, test_iterator, args)


if __name__ == "__main__":
    main()

