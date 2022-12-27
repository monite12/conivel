from typing import Any, Dict, List, Literal, Optional, Type, Union, cast
import random, functools
from dataclasses import dataclass
import nltk
from sacred.run import Run
import numpy as np
import torch
from torch.utils.data import Dataset, DataLoader
from transformers import BertForTokenClassification, BertForSequenceClassification, BertTokenizerFast, DataCollatorWithPadding  # type: ignore
from transformers.tokenization_utils_base import BatchEncoding
from tqdm import tqdm
from sklearn.metrics import r2_score
from rank_bm25 import BM25Okapi
from conivel.datas import NERSentence
from conivel.datas.dataset import NERDataset
from conivel.utils import flattened, get_tokenizer, bin_weighted_mse_loss
from conivel.predict import predict


@dataclass
class ContextRetrievalMatch:
    sentence: NERSentence
    sentence_idx: int
    side: Literal["left", "right"]
    score: Optional[float]


class ContextRetriever:
    """
    :ivar sents_nb: maximum number of sents to retrieve
    """

    def __init__(self, sents_nb: Union[int, List[int]], **kwargs) -> None:
        self.sents_nb = sents_nb

    def __call__(self, dataset: NERDataset, silent: bool = True) -> NERDataset:
        """retrieve context for each sentence of a :class:`NERDataset`"""
        new_docs = []
        for document in tqdm(dataset.documents, disable=silent):
            new_doc = []
            for sent_i, sent in enumerate(document):
                retrieval_matchs = self.retrieve(sent_i, document)
                new_doc.append(
                    NERSentence(
                        sent.tokens,
                        sent.tags,
                        [m.sentence for m in retrieval_matchs if m.side == "left"],
                        [m.sentence for m in retrieval_matchs if m.side == "right"],
                    )
                )
            new_docs.append(new_doc)
        return NERDataset(new_docs, tags=dataset.tags, tokenizer=dataset.tokenizer)

    def retrieve(
        self, sent_idx: int, document: List[NERSentence]
    ) -> List[ContextRetrievalMatch]:
        """Select context for a sentence in a document

        :param sent_idx: the index of the sentence in the document
        :param document: document in where to find the context
        """
        raise NotImplemented


def sent_with_ctx_from_matchs(
    sent: NERSentence, ctx_matchs: List[ContextRetrievalMatch]
) -> List[NERSentence]:
    return [
        NERSentence(
            sent.tokens,
            sent.tags,
            left_context=[ctx_match.sentence] if ctx_match.side == "left" else [],
            right_context=[ctx_match.sentence] if ctx_match.side == "right" else [],
        )
        for ctx_match in ctx_matchs
    ]


class RandomContextRetriever(ContextRetriever):
    """A context selector choosing context at random in a document."""

    def retrieve(
        self, sent_idx: int, document: List[NERSentence]
    ) -> List[ContextRetrievalMatch]:
        if isinstance((sents_nb := self.sents_nb), list):
            sents_nb = random.choice(sents_nb)

        selected_sents_idx = random.sample(
            [i for i in range(len(document)) if not i == sent_idx],
            k=min(len(document) - 1, sents_nb),
        )
        selected_sents_idx = sorted(selected_sents_idx)

        return [
            ContextRetrievalMatch(
                document[i], i, "left" if i < sent_idx else "right", None
            )
            for i in selected_sents_idx
        ]


class SameNounRetriever(ContextRetriever):
    """A context selector that randomly choose a sentence having a
    common name with the current sentence.

    """

    def __init__(self, sents_nb: Union[int, List[int]]):
        """
        :param sents_nb: number of context sentences to select.  If a
            list, the number of context sentences to select will be
            picked randomly among this list at call time.
        """
        # nltk pos tagging dependency
        nltk.download("averaged_perceptron_tagger")
        super().__init__(sents_nb)

    def retrieve(
        self, sent_idx: int, document: List[NERSentence]
    ) -> List[ContextRetrievalMatch]:
        if isinstance((sents_nb := self.sents_nb), list):
            sents_nb = random.choice(sents_nb)

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
            selected_sents_idx, k=min(sents_nb, len(selected_sents_idx))
        )
        selected_sents_idx = sorted(selected_sents_idx)

        return [
            ContextRetrievalMatch(
                document[i], i, "left" if i < sent_idx else "right", None
            )
            for i in selected_sents_idx
        ]


class NeighborsContextRetriever(ContextRetriever):
    """A context selector that chooses nearby sentences."""

    def __init__(self, sents_nb: Union[int, List[int]]):
        """
        :param left_sents_nb: number of left context sentences to select
        :param right_sents_nb: number of right context sentences to select
        """
        if isinstance(sents_nb, int):
            assert sents_nb % 2 == 0
        elif isinstance(sents_nb, list):
            assert all([nb % 2 == 0 for nb in sents_nb])

        super().__init__(sents_nb)

    def retrieve(
        self, sent_idx: int, document: List[NERSentence]
    ) -> List[ContextRetrievalMatch]:
        if isinstance((sents_nb := self.sents_nb), list):
            sents_nb = random.choice(sents_nb)

        left_sents_nb = sents_nb // 2
        right_sents_nb = sents_nb // 2

        left_ctx = [
            ContextRetrievalMatch(document[i], i, "left", None)
            for i in range(max(0, sent_idx - left_sents_nb), sent_idx)
        ]
        right_ctx = [
            ContextRetrievalMatch(document[i], i, "right", None)
            for i in range(sent_idx + 1, sent_idx + 1 + right_sents_nb)
        ]

        return left_ctx + right_ctx


class LeftContextRetriever(ContextRetriever):
    """"""

    def __init__(self, sents_nb: Union[int, List[int]]):
        super().__init__(sents_nb)

    def retrieve(
        self, sent_idx: int, document: List[NERSentence]
    ) -> List[ContextRetrievalMatch]:
        if isinstance((sents_nb := self.sents_nb), list):
            sents_nb = random.choice(sents_nb)

        return [
            ContextRetrievalMatch(document[i], i, "left", None)
            for i in range(max(0, sent_idx - sents_nb), sent_idx)
        ]


class RightContextRetriever(ContextRetriever):
    """"""

    def __init__(self, sents_nb: Union[int, List[int]]):
        super().__init__(sents_nb)

    def retrieve(
        self, sent_idx: int, document: List[NERSentence]
    ) -> List[ContextRetrievalMatch]:
        if isinstance((sents_nb := self.sents_nb), list):
            sents_nb = random.choice(sents_nb)

        return [
            ContextRetrievalMatch(document[i], i, "right", None)
            for i in range(sent_idx + 1, sent_idx + 1 + sents_nb)
        ]


class BM25ContextRetriever(ContextRetriever):
    """A context selector that selects sentences according to BM25 ranking formula."""

    def __init__(self, sents_nb: Union[int, List[int]]) -> None:
        """
        :param sents_nb: number of context sentences to select.  If a
            list, the number of context sentences to select will be
            picked randomly among this list at call time.
        """
        super().__init__(sents_nb)

    @staticmethod
    def _get_bm25_model(document: List[NERSentence]) -> BM25Okapi:
        return BM25Okapi([sent.tokens for sent in document])

    def retrieve(
        self, sent_idx: int, document: List[NERSentence]
    ) -> List[ContextRetrievalMatch]:
        if isinstance((sents_nb := self.sents_nb), list):
            sents_nb = random.choice(sents_nb)

        bm25_model = BM25ContextRetriever._get_bm25_model(document)
        query = document[sent_idx].tokens
        sent_scores = bm25_model.get_scores(query)
        sent_scores[sent_idx] = -1  # don't retrieve self
        topk_values, topk_indexs = torch.topk(
            torch.tensor(sent_scores), k=sents_nb, dim=0
        )
        return [
            ContextRetrievalMatch(
                document[index], index, "left" if index < sent_idx else "right", value
            )
            for value, index in zip(topk_values.tolist(), topk_indexs.tolist())
        ]


@dataclass(frozen=True)
class ContextRetrievalExample:
    """A context selection example, to be used for training a context selector."""

    #: sentence on which NER is performed
    sent: List[str]
    #: context to assist during prediction
    context: List[str]
    #: context side (doest the context comes from the left or the right of ``sent`` ?)
    context_side: Literal["left", "right"]
    #: usefulness of the exemple, between -1 and 1. Can be ``None``
    # when the usefulness is not known.
    usefulness: Optional[float] = None
    #: wether the prediction for the ``sent`` of this example was
    # correct or not before applying ``context``. Is ``None`` when not
    # applicable.
    sent_was_correctly_predicted: Optional[bool] = None

    def __hash__(self) -> int:
        return hash(
            (
                tuple(self.sent),
                tuple(self.context),
                self.context_side,
                self.usefulness,
                self.sent_was_correctly_predicted,
            )
        )


class ContextRetrievalDataset(Dataset):
    """"""

    def __init__(
        self,
        examples: List[ContextRetrievalExample],
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

    def labels(self) -> Optional[List[float]]:
        if any([ex.usefulness is None for ex in self.examples]):
            return None
        return [ex.usefulness for ex in self.examples]  # type: ignore


class NeuralContextRetriever(ContextRetriever):
    """A context selector powered by BERT"""

    def __init__(
        self,
        pretrained_model: Union[str, BertForSequenceClassification],
        heuristic_context_selector: str,
        heuristic_context_selector_kwargs: Dict[str, Any],
        batch_size: int,
        sents_nb: int,
        use_cache: bool = False,
        ranking_method: Literal["score", "combine_rank"] = "score",
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

        :param use_cache: if ``True``,
            :func:`NeuralContextRetriever.predict` will use an
            internal cache to speed up computations.

        :param ranking_method: the ranking method to use to retrieve
            context

                - ``'score'``: retrieve the ``sents_nb`` best examples

                - ``combine_rank``: Combine ranks of the neural
                  selector and of the underlying
                  ``heuristic_context_selector`` to return
                  ``sents_nb`` examples.only usable if the underlying
                  ``heuristic_context_selector`` returns scores.
        """
        if isinstance(pretrained_model, str):
            self.ctx_classifier = BertForSequenceClassification.from_pretrained(
                pretrained_model
            )
        else:
            self.ctx_classifier = pretrained_model
        self.ctx_classifier = cast(BertForSequenceClassification, self.ctx_classifier)

        self.tokenizer = get_tokenizer()

        selector_class = context_retriever_name_to_class[heuristic_context_selector]
        self.heuristic_context_selector = selector_class(
            **heuristic_context_selector_kwargs
        )

        self.batch_size = batch_size

        self.ranking_method = ranking_method

        self._predict_cache = {}
        self.use_cache = use_cache

        super().__init__(sents_nb)

    def clear_predict_cache_(self):
        """Clear the prediction cache"""
        self._predict_cache = {}

    def _predict_cache_get(self, x: ContextRetrievalExample) -> Optional[float]:
        return self._predict_cache.get(x)

    def _predict_cache_register_(self, x: ContextRetrievalExample, score: float):
        self._predict_cache[x] = score

    def set_heuristic_sents_nb_(self, sents_nb: int):
        self.heuristic_context_selector.sents_nb = sents_nb

    def predict(
        self,
        dataset: Union[ContextRetrievalDataset, List[ContextRetrievalExample]],
        device_str: Literal["cuda", "cpu", "auto"] = "auto",
    ) -> torch.Tensor:
        """
        :param dataset: A list of :class:`ContextSelectionExample`
        :param device_str: torch device

        :return: A tensor of shape ``(len(dataset))`` of scores, each
                 between -1 and 1
        """
        if isinstance(dataset, list):
            dataset = ContextRetrievalDataset(dataset, self.tokenizer)

        if device_str == "auto":
            device_str = "cuda" if torch.cuda.is_available() else "cpu"
        device = torch.device(device_str)
        self.ctx_classifier = self.ctx_classifier.to(device)  # type: ignore

        if self.use_cache:
            out_scores = [self._predict_cache_get(e) for e in dataset.examples]
            # restrict datset to uncached examples
            uncached_idxs = [i for i, s in enumerate(out_scores) if s is None]
            dataset = ContextRetrievalDataset(
                [e for e, s in zip(dataset.examples, out_scores) if s is None],
                tokenizer=dataset.tokenizer,
            )
            out_scores = torch.tensor(
                [s if not s is None else -1.0 for s in out_scores]
            ).to(device)
        else:
            out_scores = torch.tensor([0.0] * len(dataset.examples)).to(device)
            uncached_idxs = [i for i in range(len(dataset.examples))]

        data_collator = DataCollatorWithPadding(dataset.tokenizer)  # type: ignore
        dataloader = DataLoader(dataset, batch_size=self.batch_size, shuffle=False, collate_fn=data_collator)  # type: ignore

        # inference using self.ctx_classifier
        self.ctx_classifier = self.ctx_classifier.eval()
        with torch.no_grad():
            scores = torch.zeros((0,)).to(device)
            for X in dataloader:
                X = X.to(device)
                # out.logits is of shape (batch_size, 1)
                out = self.ctx_classifier(
                    X["input_ids"],
                    token_type_ids=X["token_type_ids"],
                    attention_mask=X["attention_mask"],
                )
                pred = torch.sigmoid(out.logits) * 2 - 1
                scores = torch.cat([scores, pred[:, 0]], dim=0)

        out_scores[uncached_idxs] = scores

        # register scores in cache
        if self.use_cache:
            for ex, score in zip(dataset.examples, out_scores):
                self._predict_cache_register_(ex, score.item())

        return out_scores

    def retrieve(
        self, sent_idx: int, document: List[NERSentence]
    ) -> List[ContextRetrievalMatch]:
        device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
        self.ctx_classifier = self.ctx_classifier.to(device)  # type: ignore

        sent = document[sent_idx]

        # get self.heuristic_retrieval_sents_nb potentially important
        # context sentences
        ctx_matchs = self.heuristic_retrieve_ctx(sent_idx, document)

        # no context retrieved by heuristic : nothing to do
        if len(ctx_matchs) == 0:
            return []

        # prepare datas for inference
        ctx_dataset = [
            ContextRetrievalExample(
                sent.tokens, ctx_match.sentence.tokens, ctx_match.side, None
            )
            for ctx_match in ctx_matchs
        ]
        scores = self.predict(ctx_dataset)

        if self.ranking_method == "score":
            topk = torch.topk(scores, min(self.sents_nb, scores.shape[0]), dim=0)  # type: ignore
            best_ctx_idxs = topk.indices[topk.values > 0].tolist()
        elif self.ranking_method == "combine_rank":
            assert all([not m.score is None for m in ctx_matchs])
            ctx_matchs_scores = torch.tensor([m.score for m in ctx_matchs])
            ctx_matchs_ranks = torch.arange(0, ctx_matchs_scores.shape[0])[
                torch.argsort(-ctx_matchs_scores)
            ]
            scores_ranks = torch.arange(0, scores.shape[0])[torch.argsort(-scores)]
            mean_ranks = torch.mean(
                torch.stack((ctx_matchs_ranks.float(), scores_ranks.float())), dim=0
            )
            best_ctx_idxs = torch.argsort(mean_ranks)[: self.sents_nb].tolist()
        else:
            raise RuntimeError(f"unknown ranking method: {self.ranking_method}")

        return [
            ContextRetrievalMatch(
                ctx_matchs[i].sentence,
                ctx_matchs[i].sentence_idx,
                ctx_matchs[i].side,
                float(scores[i].item()),
            )
            for i in best_ctx_idxs
        ]

    def heuristic_retrieve_ctx(
        self, sent_idx: int, document: List[NERSentence]
    ) -> List[ContextRetrievalMatch]:
        """Retrieve potentially useful context sentences to help
        predict sent at index ``sent_idx``.

        :param sent_idx: index of the sent for which NER predictions
            are made.
        :param document: the predicted sentence's document.
        """
        return self.heuristic_context_selector.retrieve(sent_idx, document)

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
        skip_correct: bool = False,
        _run: Optional[Run] = None,
    ) -> ContextRetrievalDataset:
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
            example.  Examples generated from a source sentence are
            kept if one of these examples usefulness is greater than
            this threshold.
        :param skip_correct: if ``True``, will skip example generation
            for sentences for which NER predictions are correct.
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

        ctx_selector_class = context_retriever_name_to_class[heuristic_context_selector]
        preliminary_ctx_selector = ctx_selector_class(
            **heuristic_context_selector_kwargs
        )

        ctx_selection_examples = []
        for sent_i, (sent, pred_tags, pred_scores) in tqdm(
            enumerate(zip(train_dataset.sents(), preds.tags, preds.scores)),
            total=len(preds.tags),
        ):
            if skip_correct and pred_tags == sent.tags:
                continue

            document = train_dataset.document_for_sent(sent_i)

            pred_error = NeuralContextRetriever._pred_error(
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
            ctx_matchs = preliminary_ctx_selector.retrieve(index_in_doc, document)
            ctx_sents = sent_with_ctx_from_matchs(sent, ctx_matchs)

            # generate examples by making new predictions with context
            # sentences
            preds_ctx = predict(
                ner_model,
                NERDataset(
                    [ctx_sents],
                    train_dataset.tags,
                    tokenizer=train_dataset.tokenizer,
                ),
                quiet=True,
                batch_size=batch_size,
                additional_outputs={"scores"},
            )
            assert not preds_ctx.scores is None

            # get usefulnesses context sides for all retrieved context
            # sentences
            usefulnesses = []
            context_sides = []
            for preds_scores_ctx, ctx_match in zip(preds_ctx.scores, ctx_matchs):
                pred_ctx_error = NeuralContextRetriever._pred_error(
                    sent, preds_scores_ctx, train_dataset.tag_to_id
                )
                usefulnesses.append(pred_error - pred_ctx_error)
                context_sides.append(ctx_match.side)

            # if one of the context usefulness is greater then
            # examples_usefulness_threshold, add all of them to the
            # list of generated examples
            if any([u > examples_usefulness_threshold for u in usefulnesses]):
                for usefulness, context_side, ctx_sent in zip(
                    usefulnesses, context_sides, ctx_sents
                ):
                    context_tokens = (
                        ctx_sent.left_context[0].tokens
                        if len(ctx_sent.left_context) == 1
                        else ctx_sent.right_context[0].tokens
                    )
                    ctx_selection_examples.append(
                        ContextRetrievalExample(
                            sent.tokens,
                            context_tokens,
                            context_side,
                            usefulness,
                            sent.tags == pred_tags,
                        )
                    )

        # logging
        if not _run is None:
            _run.log_scalar(
                "context_dataset_generation.examples_nb", len(ctx_selection_examples)
            )

            for ex in ctx_selection_examples:
                _run.log_scalar("context_dataset_generation.usefulness", ex.usefulness)

        return ContextRetrievalDataset(ctx_selection_examples)

    @staticmethod
    def balance_context_dataset(
        dataset: ContextRetrievalDataset, bins_nb: int
    ) -> ContextRetrievalDataset:
        """Balance the given dataset, by trying to make sure that:

            1. The number of examples ``sent`` that have been
               corrected by their ``context`` is equal to the number
               of examples ``sent`` that heven't been corrected by
               their ``context``.

            2. Examples bins formed using their usefulness have the
               same number of examples.

        :param dataset:
        :param bins_nb: number of bins to form

        :return: a balanced dataset
        """
        assert len(dataset) > 0

        # filtering to have the same number of examples with a correct
        # original prediction and without
        correctly_predicted = [
            ex for ex in dataset.examples if ex.sent_was_correctly_predicted
        ]
        incorrectly_predicted = [
            ex for ex in dataset.examples if not ex.sent_was_correctly_predicted
        ]
        assert len(correctly_predicted) > 0 and len(incorrectly_predicted) > 0
        min_len = min([len(correctly_predicted), len(incorrectly_predicted)])
        examples = correctly_predicted[:min_len] + incorrectly_predicted[:min_len]

        # binning
        # NOTE: assume that min usefulness is -1 and max usefulness is 1
        bins_size = 2.0 / bins_nb
        bin_right_edges = np.arange(-1 + bins_size, 1 + bins_size, bins_size)
        bins = []
        already_added_examples = set()
        for bin_right_edge in bin_right_edges:
            bin_examples = [
                ex for ex in examples if ex.usefulness < bin_right_edge  # type: ignore
            ]
            bins.append(list(set(bin_examples) - already_added_examples))
            already_added_examples = already_added_examples.union(bin_examples)

        # balancing bins. Each bin will have the number of examples of
        # the bin with the least examples.
        min_bin_len = min([len(b) for b in bins if not len(b) == 0])
        bins = [b[:min_bin_len] for b in bins]

        return ContextRetrievalDataset(flattened(bins))

    @staticmethod
    def train_context_selector(
        ctx_dataset: ContextRetrievalDataset,
        epochs_nb: int,
        batch_size: int,
        learning_rate: float,
        weights_bins_nb: Optional[int] = None,
        _run: Optional[Run] = None,
        log_full_loss: bool = False,
    ) -> BertForSequenceClassification:
        """Instantiate and train a context classifier.

        :param ner_model: an already trained NER model used to
            generate the context selection dataset.
        :param train_dataset: NER dataset used to generate the context
            selection dataset.
        :param epochs_nb: number of training epochs.
        :param batch_size:
        :param weights_bins_nb: number of loss weight bins.  If
            ``None``, the MSELoss will not be weighted.
        :param _run: current sacred run.  If not ``None``, will be
            used to record training metrics.
        :param log_full_loss: if ``True``, log the loss at each batch
            (otherwise, only log mean epochs loss)

        :return: a trained ``BertForSequenceClassification``
        """
        device = torch.device("cuda" if torch.cuda.is_available() else "cpu")

        ctx_classifier = BertForSequenceClassification.from_pretrained(
            "bert-base-cased", problem_type="regression", num_labels=1
        )  # type: ignore
        ctx_classifier = cast(BertForSequenceClassification, ctx_classifier)
        ctx_classifier = ctx_classifier.to(device)

        if not weights_bins_nb is None:
            examples_usefulnesses = torch.tensor(
                [ex.usefulness for ex in ctx_dataset.examples]
            )
            # torch.histogram is not implemented on CUDA, so we avoid
            # sending tensors to GPU until after this computation
            # (bins_nb), (bins_nb)
            bins_count, bins_edges = torch.histogram(
                examples_usefulnesses, weights_bins_nb
            )
            bins_count = bins_count.to(device)
            bins_edges = bins_edges.to(device)
            # (bins_nb)
            bins_weights = (torch.max(bins_count) + 1) / (bins_count + 1)

            loss_fn = functools.partial(
                bin_weighted_mse_loss, bins_weights=bins_weights, bins_edges=bins_edges
            )
        else:
            loss_fn = torch.nn.MSELoss()

        optimizer = torch.optim.AdamW(ctx_classifier.parameters(), lr=learning_rate)

        data_collator = DataCollatorWithPadding(ctx_dataset.tokenizer)  # type: ignore
        dataloader = DataLoader(
            ctx_dataset, batch_size=batch_size, shuffle=True, collate_fn=data_collator
        )

        for _ in range(epochs_nb):

            epoch_losses = []
            epoch_preds = []
            ctx_classifier = ctx_classifier.train()

            data_tqdm = tqdm(dataloader)
            for X in data_tqdm:

                optimizer.zero_grad()

                X = X.to(device)

                out = ctx_classifier(
                    X["input_ids"],
                    token_type_ids=X["token_type_ids"],
                    attention_mask=X["attention_mask"],
                )
                # (batch_size, 1)
                pred = torch.sigmoid(out.logits) * 2 - 1

                loss = loss_fn(pred[:, 0], X["labels"])
                loss.backward()

                optimizer.step()

                if not _run is None and log_full_loss:
                    _run.log_scalar("neural_selector_training.loss", loss.item())

                data_tqdm.set_description(f"loss : {loss.item():.3f}")
                epoch_losses.append(loss.item())

                epoch_preds += pred[:, 0].tolist()

            mean_epoch_loss = sum(epoch_losses) / len(epoch_losses)
            tqdm.write(f"epoch mean loss : {mean_epoch_loss:.3f}")
            if not _run is None:
                # mean loss
                _run.log_scalar(
                    "neural_selector_training.mean_epoch_loss", mean_epoch_loss
                )
                # r2 score
                _run.log_scalar(
                    "neural_selector_training.r2_score",
                    r2_score(ctx_dataset.labels(), epoch_preds),
                )

        return ctx_classifier


class IdealNeuralContextRetriever(ContextRetriever):
    """
    A context retriever that always return the ``sents_nb`` most
    helpful contexts retrieved by its ``preliminary_ctx_selector``
    according to its given ``ner_model``
    """

    def __init__(
        self,
        sents_nb: Union[int, List[int]],
        preliminary_ctx_selector: ContextRetriever,
        ner_model: BertForTokenClassification,
        batch_size: int,
    ) -> None:
        self.preliminary_ctx_selector = preliminary_ctx_selector
        self.ner_model = ner_model
        self.batch_size = batch_size
        super().__init__(sents_nb)

    def set_heuristic_sents_nb_(self, sents_nb: int):
        self.preliminary_ctx_selector.sents_nb = sents_nb

    def retrieve(
        self, sent_idx: int, document: List[NERSentence]
    ) -> List[ContextRetrievalMatch]:
        if isinstance((sents_nb := self.sents_nb), list):
            sents_nb = random.choice(sents_nb)

        contexts = self.preliminary_ctx_selector.retrieve(sent_idx, document)

        sent = document[sent_idx]
        sent_with_ctx = sent_with_ctx_from_matchs(sent, contexts)

        tags = {"O", "B-PER", "I-PER"}
        tag_to_id = {tag: i for i, tag in enumerate(sorted(tags))}

        ctx_preds = predict(
            self.ner_model,
            NERDataset([sent_with_ctx], tags),
            quiet=True,
            batch_size=self.batch_size,
            additional_outputs={"scores"},
        )
        assert not ctx_preds.scores is None

        context_and_err = [
            (
                context,
                NeuralContextRetriever._pred_error(sent, scores, tag_to_id),
            )
            for context, scores in zip(contexts, ctx_preds.scores)
        ]

        ok_contexts_and_err = list(sorted(context_and_err, key=lambda cd: cd[1]))[
            :sents_nb
        ]
        ok_contexts = [context for context, _ in ok_contexts_and_err]

        return ok_contexts


context_retriever_name_to_class: Dict[str, Type[ContextRetriever]] = {
    "neural": NeuralContextRetriever,
    "neighbors": NeighborsContextRetriever,
    "left": LeftContextRetriever,
    "right": RightContextRetriever,
    "bm25": BM25ContextRetriever,
    "samenoun": SameNounRetriever,
    "random": RandomContextRetriever,
}
