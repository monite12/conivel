from typing import Any, Dict, List, Literal, Optional, Tuple, Type, cast
import random
from functools import lru_cache
from dataclasses import dataclass
import nltk
from sacred.run import Run
import torch
from torch.utils.data import Dataset, DataLoader
from transformers import BertForTokenClassification, BertForSequenceClassification, BertTokenizerFast, DataCollatorWithPadding  # type: ignore
from transformers.tokenization_utils_base import BatchEncoding
from tqdm import tqdm
from rank_bm25 import BM25Okapi
from conivel.datas import NERSentence
from conivel.datas.dataset import NERDataset
from conivel.utils import get_tokenizer
from conivel.predict import predict


class ContextSelector:
    """"""

    def __init__(self, **kwargs) -> None:
        raise NotImplemented

    def __call__(
        self, sent_idx: int, document: Tuple[NERSentence, ...]
    ) -> Tuple[List[NERSentence], List[NERSentence]]:
        """Select context for a sentence in a document

        :param sent_idx: the index of the sentence in the document
        :param document: document in where to find the context

        :return: a tuple with the left and right context of the input
                 sent.  Sents must be returned *in order* according to
                 the original text.
        """
        raise NotImplemented


context_selector_name_to_class: Dict[str, Type[ContextSelector]] = {}


class RandomContextSelector(ContextSelector):
    """A context selector choosing context at random in a document."""

    def __init__(self, sents_nb):
        """
        :param sents_nb: number of context sentences to select
        """
        self.sents_nb = sents_nb

    def __call__(
        self, sent_idx: int, document: Tuple[NERSentence, ...]
    ) -> Tuple[List[NERSentence], List[NERSentence]]:
        """ """
        selected_sents_idx = random.sample(
            [i for i in range(len(document)) if not i == sent_idx],
            k=min(len(document) - 1, self.sents_nb),
        )
        selected_sents_idx = sorted(selected_sents_idx)

        return (
            [document[i] for i in selected_sents_idx if i < sent_idx],
            [document[i] for i in selected_sents_idx if i > sent_idx],
        )


context_selector_name_to_class["random"] = RandomContextSelector


class SameWordSelector(ContextSelector):
    """A context selector that randomly choose a sentence having a
    common name with the current sentence.

    """

    def __init__(self, sents_nb: int):
        self.sents_nb = sents_nb
        # nltk pos tagging dependency
        nltk.download("averaged_perceptron_tagger")

    def __call__(
        self, sent_idx: int, document: Tuple[NERSentence, ...]
    ) -> Tuple[List[NERSentence], List[NERSentence]]:
        """ """
        sent = document[sent_idx]
        tagged = nltk.pos_tag(sent.tokens)
        name_tokens = set([t[0] for t in tagged if t[1].startswith("NN")])

        # other sentences from the document with at least one token
        # from sent
        selected_sents_idx = [
            i
            for i, s in enumerate(document)
            if not i == sent_idx and len(name_tokens.intersection(set(s.tokens))) > 0
        ]

        # keep at most k sentences
        selected_sents_idx = random.sample(
            selected_sents_idx, k=min(self.sents_nb, len(selected_sents_idx))
        )
        selected_sents_idx = sorted(selected_sents_idx)

        return (
            [document[i] for i in selected_sents_idx if i < sent_idx],
            [document[i] for i in selected_sents_idx if i > sent_idx],
        )


context_selector_name_to_class["sameword"] = SameWordSelector


class NeighborsContextSelector(ContextSelector):
    """A context selector that chooses nearby sentences."""

    def __init__(self, left_sents_nb: int, right_sents_nb: int) -> None:
        """
        :param left_sents_nb: number of left context sentences to select
        :param right_sents_nb: number of right context sentences to select
        """
        self.left_sents_nb = left_sents_nb
        self.right_sents_nb = right_sents_nb

    def __call__(
        self, sent_idx: int, document: Tuple[NERSentence, ...]
    ) -> Tuple[List[NERSentence], List[NERSentence]]:
        """ """
        return (
            list(document[max(0, sent_idx - self.left_sents_nb) : sent_idx]),
            list(document[sent_idx + 1 : sent_idx + 1 + self.right_sents_nb]),
        )


context_selector_name_to_class["neighbors"] = NeighborsContextSelector


class BM25ContextSelector(ContextSelector):
    """A context selector that selects sentences according to BM25 ranking formula."""

    def __init__(self, sents_nb: int) -> None:
        self.sents_nb = sents_nb

    @staticmethod
    @lru_cache(maxsize=None)
    def _get_bm25_model(document: Tuple[NERSentence, ...]) -> BM25Okapi:
        return BM25Okapi([sent.tokens for sent in document])

    def __call__(
        self, sent_idx: int, document: Tuple[NERSentence, ...]
    ) -> Tuple[List[NERSentence], List[NERSentence]]:
        """"""
        bm25_model = BM25ContextSelector._get_bm25_model(document)
        query = document[sent_idx].tokens
        sent_scores = bm25_model.get_scores(query)
        sent_scores[sent_idx] = -1  # don't retrieve self
        best_idxs = list(
            torch.topk(torch.tensor(sent_scores), k=self.sents_nb, dim=0)
            .indices.sort()
            .values
        )
        return (
            [document[i] for i in best_idxs if i < sent_idx],
            [document[i] for i in best_idxs if i > sent_idx],
        )


context_selector_name_to_class["bm25"] = BM25ContextSelector


@dataclass
class ContextSelectionExample:
    """A context selection example, to be used for training a context selector."""

    #: sentence on which NER is performed
    sent: List[str]
    #: context to assist during prediction
    context: List[str]
    #: context side (doest the context comes from the left or the right of ``sent`` ?)
    context_side: Literal["left", "right"]
    #: usefulness of the exemple, between -1 and 1.
    usefulness: Optional[float]


class ContextSelectionDataset(Dataset):
    """"""

    def __init__(
        self,
        examples: List[ContextSelectionExample],
        tokenizer: Optional[BertTokenizerFast] = None,
    ) -> None:
        self.examples = examples
        if tokenizer is None:
            tokenizer = get_tokenizer()
        self.tokenizer: BertTokenizerFast = tokenizer

    def __len__(self) -> int:
        return len(self.examples)

    def __getitem__(self, index: int) -> BatchEncoding:
        """Get a BatchEncoding representing example at index.

        :param index: index of the example to retrieve

        :return: a ``BatchEncoding``, with key ``'label'`` set.
        """
        example = self.examples[index]

        if example.context_side == "left":
            tokens = example.context + ["<"] + example.sent
        elif example.context_side == "right":
            tokens = example.sent + [">"] + example.context
        else:
            raise ValueError

        batch: BatchEncoding = self.tokenizer(
            tokens,
            is_split_into_words=True,
            truncation=True,
            max_length=512,
        )

        if not example.usefulness is None:
            batch["label"] = example.usefulness

        return batch

    def to_jsonifiable(self) -> List[dict]:
        return [vars(example) for example in self.examples]


class NeuralContextSelector(ContextSelector):
    """A context selector powered by BERT"""

    def __init__(
        self,
        pretrained_model_name: str,
        heuristic_context_selector: str,
        heuristic_context_selector_kwargs: Dict[str, Any],
        batch_size: int,
        sents_nb: int,
    ) -> None:
        """
        :param pretrained_model_name: pretrained model name, used to
            load a :class:`transformers.BertForSequenceClassification`
        :param heuristic_context_selector: name of the context
            selector to use as retrieval heuristic, from
            ``context_selector_name_to_class``
        :param heuristic_context_selector_kwargs: kwargs to pass the
            heuristic context retriever at instantiation time
        :param batch_size: batch size used at inference
        :param sents_nb: max number of sents to retrieve
        """
        self.ctx_classifier = BertForSequenceClassification.from_pretrained(
            pretrained_model_name
        )
        self.ctx_classifier = cast(BertForSequenceClassification, self.ctx_classifier)
        self.tokenizer = get_tokenizer()

        selector_class = context_selector_name_to_class[heuristic_context_selector]
        self.heuristic_context_selector = selector_class(
            **heuristic_context_selector_kwargs
        )

        self.batch_size = batch_size
        self.sents_nb = sents_nb

    @lru_cache(maxsize=None)
    def __call__(
        self, sent_idx: int, document: Tuple[NERSentence, ...]
    ) -> Tuple[List[NERSentence], List[NERSentence]]:
        """"""
        sent = document[sent_idx]

        # get self.heuristic_retrieval_sents_nb potentially important
        # context sentences
        left_ctx, right_ctx = self.heuristic_retrieve_ctx(sent_idx, document)
        ctx_sents = left_ctx + right_ctx

        # no context retrieved by heuristic : nothing to do
        if len(ctx_sents) == 0:
            return ([], [])

        # prepare datas for inference
        dataset = ContextSelectionDataset(
            [
                ContextSelectionExample(sent.tokens, ctx_sent.tokens, "left", None)
                for ctx_sent in left_ctx
            ]
            + [
                ContextSelectionExample(sent.tokens, ctx_sent.tokens, "right", None)
                for ctx_sent in right_ctx
            ],
            self.tokenizer,
        )
        data_collator = DataCollatorWithPadding(dataset.tokenizer)  # type: ignore
        dataloader = DataLoader(
            dataset, batch_size=self.batch_size, shuffle=False, collate_fn=data_collator
        )

        # inference using self.ctx_classifier
        self.ctx_classifier = self.ctx_classifier.eval()
        with torch.no_grad():
            scores = torch.zeros((0,))
            for X in dataloader:
                # out.logits is of shape (batch_size, 1)
                out = self.ctx_classifier(
                    X["input_ids"],
                    token_type_ids=X["token_type_ids"],
                    attention_mask=X["attention_mask"],
                )
                scores = torch.cat([scores, out.logits[:, 0]], dim=0)

            # now scores should be of shape
            # (self.heuristic_retrieval_sents_nb). We keep the top
            # `self.sents_nb` sentences that are positive.
            topk = torch.topk(scores, min(self.sents_nb, scores.shape[0]), dim=0)
            best_ctx_idxs = topk.indices[topk.values > 0]
            left_ctx_idxs_mask = best_ctx_idxs < len(left_ctx)

            left_ctx_idxs = best_ctx_idxs[left_ctx_idxs_mask].sort().values
            right_ctx_idxs = best_ctx_idxs[~left_ctx_idxs_mask].sort().values

        return (
            [ctx_sents[ctx_idx] for ctx_idx in left_ctx_idxs],
            [ctx_sents[ctx_idx] for ctx_idx in right_ctx_idxs],
        )

    def heuristic_retrieve_ctx(
        self, sent_idx: int, document: Tuple[NERSentence, ...]
    ) -> Tuple[List[NERSentence], List[NERSentence]]:
        """Retrieve potentially useful context sentences to help
        predict sent at index ``sent_idx``.

        :param sent_idx: index of the sent for which NER predictions
            are made.
        :param document: the predicted sentence's document.

        :return: ``(left_context, right_context)``
        """
        return self.heuristic_context_selector(sent_idx, document)

    @staticmethod
    def _pred_error(
        sent: NERSentence, pred_scores: torch.Tensor, tag_to_id: Dict[str, int]
    ) -> float:
        """Compute error between a reference sentence and a prediction

        :param sent: reference sentence
        :param pred_scores: ``(sentence_size, vocab_size)``
        :param tag_to_id: a mapping from a tag to its id in the
            vocabulary.

        :return: an error between 0 and 1
        """
        errs = []
        for tag_i, tag in enumerate(sent.tags):
            tag_score = pred_scores[tag_i][tag_to_id[tag]].item()
            errs.append(1 - tag_score)
        return max(errs)

    @staticmethod
    def generate_context_dataset(
        ner_model: BertForTokenClassification,
        train_dataset: NERDataset,
        batch_size: int,
        heuristic_context_selector: str,
        heuristic_context_selector_kwargs: Dict[str, Any],
        max_examples_nb: Optional[int] = None,
        examples_usefulness_threshold: float = 0.0,
        _run: Optional[Run] = None,
    ) -> ContextSelectionDataset:
        """Generate a context selection training dataset.

        The process is as follows :

            1. Make predictions for a NER dataset using an already
               trained NER model.

            2. For each prediction, sample a bunch of possible context
               sentences using some heuristic, and retry predictions
               with those context for the sentence.  Then, the
               difference of errors between the prediction without
               context and the prediction with context is used to
               create a sample of context retrieval.

        .. note::

            For now, uses ``SameWordSelector`` as sampling heuristic.

        :todo: make a choice on heuristic

        :param ner_model: an already trained NER model used to
            generate initial predictions
        :param train_dataset: NER dataset used to extract examples
        :param batch_size: batch size used for NER inference
        :param heuristic_context_selector: name of the context
            selector to use as retrieval heuristic, from
            ``context_selector_name_to_class``
        :param heuristic_context_selector_kwargs: kwargs to pass the
            heuristic context retriever at instantiation time
        :param max_examples_nb: max number of examples in the
            generated dataset.  If ``None``, no limit is applied.
        :param examples_usefulness_threshold: threshold to select
            example.  an example is considered only if the absolute
            value of its usefulness is greater or equal to this
            threshold.
        :param _run: The current sacred run.  If not ``None``, will be
            used to record generation metrics.

        :return: a ``ContextSelectionDataset`` that can be used to
                 train a context selector.
        """
        preds = predict(
            ner_model,
            train_dataset,
            batch_size=batch_size,
            additional_outputs={"scores"},
        )
        assert not preds.scores is None

        ctx_selector_class = context_selector_name_to_class[heuristic_context_selector]
        preliminary_ctx_selector = ctx_selector_class(
            **heuristic_context_selector_kwargs
        )

        ctx_selection_examples = []
        for sent_i, (sent, pred_scores) in tqdm(
            enumerate(zip(train_dataset.sents(), preds.scores)), total=len(preds.tags)
        ):
            document = train_dataset.document_for_sent(sent_i)

            pred_error = NeuralContextSelector._pred_error(
                sent, pred_scores, train_dataset.tag_to_id
            )

            # did we already retrieve enough examples ?
            if (
                not max_examples_nb is None
                and len(ctx_selection_examples) >= max_examples_nb
            ):
                ctx_selection_examples = ctx_selection_examples[:max_examples_nb]
                break

            # retrieve n context sentences
            index_in_doc = train_dataset.sent_document_index(sent_i)
            left_ctx_sents, right_ctx_sents = preliminary_ctx_selector(
                index_in_doc, tuple(document)
            )
            sent_and_ctx = [
                NERSentence(sent.tokens, sent.tags, left_context=[ctx_sent])
                for ctx_sent in left_ctx_sents
            ]
            sent_and_ctx += [
                NERSentence(sent.tokens, sent.tags, right_context=[ctx_sent])
                for ctx_sent in right_ctx_sents
            ]

            # generate examples by making new predictions with context
            # sentences
            preds_ctx = predict(
                ner_model,
                NERDataset([sent_and_ctx], train_dataset.tags),
                quiet=True,
                batch_size=batch_size,
                additional_outputs={"scores"},
            )
            assert not preds_ctx.scores is None
            for i, (preds_scores_ctx, ctx_sent) in enumerate(
                zip(preds_ctx.scores, left_ctx_sents + right_ctx_sents)
            ):
                pred_ctx_error = NeuralContextSelector._pred_error(
                    sent, preds_scores_ctx, train_dataset.tag_to_id
                )
                usefulness = pred_error - pred_ctx_error
                if abs(usefulness) >= examples_usefulness_threshold:
                    context_side = "left" if i < len(left_ctx_sents) else "right"
                    ctx_selection_examples.append(
                        ContextSelectionExample(
                            sent.tokens, ctx_sent.tokens, context_side, usefulness
                        )
                    )

        if not _run is None:
            _run.log_scalar(
                "context_dataset_generation.examples_nb", len(ctx_selection_examples)
            )

            for ex in ctx_selection_examples:
                _run.log_scalar("context_dataset_generation.usefulness", ex.usefulness)

        return ContextSelectionDataset(ctx_selection_examples)

    @staticmethod
    def train_context_selector(
        ctx_dataset: ContextSelectionDataset,
        epochs_nb: int,
        batch_size: int,
        learning_rate: float,
        _run: Optional[Run] = None,
    ) -> BertForSequenceClassification:
        """Instantiate and train a context classifier.

        :param ner_model: an already trained NER model used to
            generate the context selection dataset.
        :param train_dataset: NER dataset used to generate the context
            selection dataset.
        :param epochs_nb: number of training epochs.
        :param batch_size:
        :param _run: current sacred run.  If not ``None``, will be
            used to record training metrics.

        :return: a trained ``BertForSequenceClassification``
        """
        ctx_classifier = BertForSequenceClassification.from_pretrained(
            "bert-base-cased", problem_type="regression", num_labels=1
        )  # type: ignore
        ctx_classifier = cast(BertForSequenceClassification, ctx_classifier)

        optimizer = torch.optim.AdamW(ctx_classifier.parameters(), lr=learning_rate)

        data_collator = DataCollatorWithPadding(ctx_dataset.tokenizer)  # type: ignore
        dataloader = DataLoader(
            ctx_dataset, batch_size=batch_size, shuffle=True, collate_fn=data_collator
        )

        for _ in range(epochs_nb):

            epoch_losses = []
            ctx_classifier = ctx_classifier.train()

            data_tqdm = tqdm(dataloader)
            for X in data_tqdm:

                optimizer.zero_grad()

                out = ctx_classifier(
                    X["input_ids"],
                    token_type_ids=X["token_type_ids"],
                    attention_mask=X["attention_mask"],
                    labels=X["labels"],
                )
                out.loss.backward()
                optimizer.step()

                if not _run is None:
                    _run.log_scalar("neural_selector_training.loss", out.loss.item())

                data_tqdm.set_description(f"loss : {out.loss.item():.3f}")
                epoch_losses.append(out.loss.item())

            mean_epoch_loss = sum(epoch_losses) / len(epoch_losses)
            tqdm.write(f"epoch mean loss : {mean_epoch_loss:.3f}")
            if not _run is None:
                _run.log_scalar(
                    "neural_selector_training.mean_epoch_loss", mean_epoch_loss
                )

        return ctx_classifier


context_selector_name_to_class["neural"] = NeuralContextSelector
