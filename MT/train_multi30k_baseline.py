import os
import json
import math
import time
import argparse
import logging
import random

import pickle
from tqdm import tqdm
import spacy
import numpy as np

import torch
import torch.nn as nn
import torch.optim as optim
import torch.nn.functional as F
from torchtext.data.metrics import bleu_score
from torchtext.datasets import Multi30k

import Transformer_baseline
from helper import *
from Multi30kDataLoader import Multi30kDataLoader

import nltk

torch.cuda.empty_cache()
device = torch.device('cuda' if torch.cuda.is_available() else 'cpu')

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

def generate_square_subsequent_mask(sz):
    mask = (torch.triu(torch.ones((sz, sz), device=device)) == 1).transpose(0, 1)
    mask = mask.float().masked_fill(mask == 0, float('-inf')).masked_fill(mask == 1, float(0.0))
    return mask


def create_mask(src, tgt):
    src_seq_len = src.shape[0]
    tgt_seq_len = tgt.shape[0]

    tgt_mask = generate_square_subsequent_mask(tgt_seq_len)
    src_mask = torch.zeros((src_seq_len, src_seq_len),device=device).type(torch.bool)

    src_padding_mask = (src == PAD_IDX).transpose(0, 1)
    tgt_padding_mask = (tgt == PAD_IDX).transpose(0, 1)
    return src_mask, tgt_mask, src_padding_mask, tgt_padding_mask

def epoch_time(start_time, end_time):
    elapsed_time = end_time - start_time
    elapsed_mins = int(elapsed_time / 60)
    elapsed_secs = int(elapsed_time - (elapsed_mins * 60))
    return elapsed_mins, elapsed_secs



def translate_sentence(tokens, src_field, trg_field, model, max_len=2000, logging=True):
    model.eval()  # change into the evaluation mode

    # append <sos> token at the beginning and <eos> token at the end
    tokens = [special_symbols[2]] + tokens + [special_symbols[3]]
    if logging:
        print(f"full source token: {tokens}")

    # print("Here: ", src_field)
    src_indexes = [src_field[token] for token in tokens]
    if logging:
        print(f"source sentence index: {src_indexes}")

    src_tensor = torch.LongTensor(src_indexes).unsqueeze(0).transpose(0,1).to(device)
    num_tokens = src_tensor.shape[0]
    src_mask = (torch.zeros(num_tokens, num_tokens)).type(torch.bool).to(device)

    with torch.no_grad():
        memory = model.encode(src_tensor, src_mask)

    trg_indexes = [BOS_IDX]

    ys = torch.ones(1, 1).fill_(BOS_IDX).type(torch.long).to(device)

    with torch.no_grad():
        for i in range(max_len-1):
            # generate our output
            memory = memory.to(device)
            tgt_mask = (generate_square_subsequent_mask(ys.size(0))
                        .type(torch.bool)).to(device)
            out = model.decode(ys, memory, tgt_mask)
            out = out.transpose(0, 1)
            prob = model.generator(out[:, -1])
            _, next_word = torch.max(prob, dim=1)
            next_word = next_word.item()

            # trg_indexes.append(pred_token)  # add to output statement
            ys = torch.cat([ys,
                            torch.ones(1, 1).type_as(src_tensor.data).fill_(next_word)], dim=0)
            # The moment you meet <eos>, it ends
            if next_word == EOS_IDX:
                break

    #  Convert each output word index to an actual word
    trg_tokens = [trg_field.get_itos()[i] for i in ys]

    # Returns the output statementz excluding the first <sos>
    return trg_tokens[1:]


def show_bleu(iterator, data, src_field, trg_field, model, device, max_len=50):
    index = 0
    trgs = []
    pred_trgs = []
    src_tokenizer, trg_tokenizer = iterator.get_tokenizer()

    for datum in data:
        src, trg = datum
        trg_tok = trg_tokenizer(trg.lower())
        src_tok = src_tokenizer(src.lower())

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


def cal_loss(pred, gold, trg_pad_idx, smoothing=True):
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

    # iterate through whole data set
    for i, batch in enumerate(iterator):
        src, trg = batch

        src = src.transpose(0,1).to(device)
        trg = trg.transpose(0,1).to(device)

        trg_in = trg[:-1,:].clone().to(device)
        trg_y = trg[1:,:].clone().to(device)

        # exclude the end of the sentence
        # start with the beginning of the sentence

        src_mask, tgt_mask, src_padding_mask, tgt_padding_mask = create_mask(src, trg_in)
        output = model(src, trg_in, src_mask, tgt_mask, src_padding_mask,
                       tgt_padding_mask, src_padding_mask)

        optimizer.optimizer.zero_grad()
        # output: [batch size, trg_len - 1, output_dim]
        # trg: [batch size, trg_len]
        output_dim = output.shape[-1]
        output = output.contiguous().view(-1, output_dim)
        # Exclude index 0 (<sos>) of output word
        trg_y = trg_y.contiguous().view(-1)

        # calculate the loss
        loss = cal_loss(output, trg_y, PAD_IDX, True)
        loss.backward()
        # gradient clipping
        torch.nn.utils.clip_grad_norm_(model.parameters(), clip)
        # update the parameters
        optimizer.step()
        # calculate total loss
        epoch_loss += loss.item()
        if (i+1) % log_iter == 0 or i==0:
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

            src = src.transpose(0,1).to(device)
            trg = trg.transpose(0,1).to(device)

            trg_in = trg[:-1,:].clone().to(device)
            trg_y = trg[1:,:].clone().to(device)

            # exclude the end of the sentence
            # start with the beginning of the sentence
            src_mask, tgt_mask, src_padding_mask, tgt_padding_mask = create_mask(src, trg_in)

            output = model(src, trg_in, src_mask, tgt_mask, src_padding_mask,
                           tgt_padding_mask, src_padding_mask)

            # output: [batch size, trg_len - 1, output_dim]
            # trg: [batch size, trg_len]
            output_dim = output.shape[-1]

            output = output.contiguous().view(-1, output_dim)
            # Exclude index 0 (<sos>) of output word
            trg_y = trg_y.contiguous().view(-1)

            # output: [batch size * trg_len - 1, output_dim]
            # trg: [batch size * trg len - 1]

            # calculate the loss
            loss = cal_loss(output, trg_y, PAD_IDX, smoothing=False)

            # calculate total loss
            epoch_loss += loss.item()

    return epoch_loss / len(iterator)


def train_model(model, train_iterator, valid_iterator, optimizer, n_epochs, clip, args):

    # save the losses
    log = []
    # ignore the padding index
    criterion = nn.CrossEntropyLoss(ignore_index=PAD_IDX)
    best_valid_loss = float('inf')

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
                     + f' train_loss: {train_loss:.3f} train_ppl: {train_ppl} |'
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
        # dump into json file
        log_name = os.path.join(args.log_dir, 'train.log.json')
        with open(log_name, 'w') as f:
            json.dump(log, f)


def eval_model(model, data_iterator, dataset, args, src_vocab, trg_vocab):
    log = []
    criterion = nn.CrossEntropyLoss(ignore_index=PAD_IDX)
    loss = evaluate(model, data_iterator, criterion)
    ppl = math.exp(min(loss, 100))
    logging.info(f' test_loss: {loss:.3f} | test_PPL: {ppl} ')

    bleu, bleu1, bleu2, bleu3, bleu4 = show_bleu(data_iterator,
                     dataset, src_vocab, trg_vocab, model, device)
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
    parser.add_argument("--batch_size", type=int, default=128)
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


    train_iterator = Multi30kDataLoader(
        'en', 'de', 'train', batch_size=args.batch_size, num_workers=args.num_workers, use_bpe=args.use_bpe, shuffle=True)
    valid_iterator = Multi30kDataLoader(
        'en', 'de', 'valid', batch_size=args.batch_size, num_workers=args.num_workers, use_bpe=args.use_bpe, shuffle=True)
    test_iterator = Multi30kDataLoader(
        'en', 'de', 'test', batch_size=args.batch_size, num_workers=args.num_workers, use_bpe=args.use_bpe, shuffle=True)
    logging.info(f"training dataset: {train_iterator.data_size} \n"
                    + f"validation dataset: {valid_iterator.data_size} \n"
                    + f"Test dataset: {test_iterator.data_size} \n")

    src_vocab_dim = len(train_iterator.vocab['src'])
    trg_vocab_dim = len(train_iterator.vocab['trg'])

    logging.info(f"src_vocab: {src_vocab_dim} \n" +
                    f"trg_vocab: {trg_vocab_dim} \n")
    logging.info(f"Running on: {device}")

    # define padding index
    SRC_PAD_IDX = PAD_IDX
    TRG_PAD_IDX = PAD_IDX

    # config
    INPUT_DIM = src_vocab_dim
    OUTPUT_DIM = trg_vocab_dim
    HIDDEN_DIM = 512
    ENC_LAYERS = 6
    DEC_LAYERS = 6
    ENC_HEADS = 8
    DEC_HEADS = 8
    ENC_PF_DIM = 2048
    DEC_PF_DIM = 2048
    ENC_DROPOUT = 0.1
    DEC_DROPOUT = 0.1
    warmup = 4000


    def count_parameters(model):
        return sum(p.numel() for p in model.parameters() if p.requires_grad)

    def initialize_weights(m):
        if hasattr(m, 'weight') and m.weight.dim() > 1:
            nn.init.xavier_uniform_(m.weight.data)
        if hasattr(m, 'bias') and m.bias is not None:
            nn.init.constant_(m.bias, 0)

    if not args.eval_only:
        N_EPOCHS = 10
        CLIP = 1

        logging.info("Running train")

        model = Transformer_baseline.Seq2SeqTransformer(ENC_LAYERS, DEC_LAYERS, HIDDEN_DIM,ENC_HEADS, INPUT_DIM, OUTPUT_DIM, ENC_PF_DIM).to(device)

        logging.info(model)
        logging.info(
            f'Created model: The model has {count_parameters(model)} trainable parameters')
        model.apply(initialize_weights)
        # Adam optimizer with lr scheduling
        optimizer = NoamOpt(HIDDEN_DIM, 1, warmup,
                    torch.optim.Adam(model.parameters(), betas=(0.9, 0.98), eps=1e-9))
        # LEARNING_RATE = 0.0005
        # optimizer = torch.optim.Adam(model.parameters(), lr=LEARNING_RATE, betas=(0.9, 0.98), eps=1e-9)
        train_model(model, train_iterator, valid_iterator,
                    optimizer, N_EPOCHS, CLIP, args)

        logging.info("Running eval")
        eval_model(model, test_iterator,test_iterator.dataset, args,train_iterator.vocab['src'],train_iterator.vocab['trg'])
    else:
        logging.info(f"Eval only")

        model = Transformer_baseline.Seq2SeqTransformer(ENC_LAYERS, DEC_LAYERS, HIDDEN_DIM,ENC_HEADS, INPUT_DIM, OUTPUT_DIM, ENC_PF_DIM).to(device)

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
        eval_model(model, test_iterator,test_iterator.dataset, args,test_iterator.vocab['src'],test_iterator.vocab['trg'])


if __name__ == "__main__":
    main()
