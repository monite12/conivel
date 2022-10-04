from typing import List, Optional
import os, glob, re
from conivel.datas import NERSentence
from conivel.datas.dataset import NERDataset


script_dir = os.path.dirname(os.path.abspath(__file__))

book_groups = {
    "fantasy": {
        "TheFellowshipoftheRing",
        "TheWheelOfTime",
        "TheWayOfShadows",
        "TheBladeItself",
        "Elantris",
        "ThePaintedMan",
        "GardensOfTheMoon",
        "Magician",
        "BlackPrism",
        "TheBlackCompany",
        "Mistborn",
        "AGameOfThrones",
        "AssassinsApprentice",
        "TheNameOfTheWind",
        "TheColourOfMagic",
        "TheWayOfKings",
        "TheLiesOfLockeLamora",
    }
}


class DekkerDataset(NERDataset):
    """"""

    def __init__(
        self,
        directory: Optional[str] = None,
        book_group: Optional[str] = None,
        **kwargs,
    ):
        """"""
        if directory is None:
            directory = f"{script_dir}/dataset"

        new_paths = glob.glob(f"{directory}/new/*.conll.fixed")
        old_paths = glob.glob(f"{directory}/old/*.conll.fixed")

        def book_name(path: str) -> str:
            return re.search(r"[^.]*", (os.path.basename(path))).group(0)  # type: ignore

        documents = []

        for book_path in new_paths + old_paths:

            # skip book if it's not in the given book group
            if not book_group is None:
                name = book_name(book_path)
                if not name in book_groups[book_group]:
                    continue

            # load tokens and tags from CoNLL formatted file
            tokens = []
            tags = []

            with open(book_path) as f:

                for i, line in enumerate(f):

                    try:
                        token, tag = line.strip().split(" ")
                    except ValueError:
                        print(f"error processing line {i+1} of book {book_path}")
                        print(f"line content was : '{line}'")
                        print("trying to proceed...")
                        continue

                    tokens.append(token)
                    tags.append(tag)

            # parse into sentences
            doc = []
            sent = NERSentence()

            for i, (token, tag) in enumerate(zip(tokens, tags)):

                fixed_token = '"' if token in {"``", "''"} else token
                fixed_token = "'" if token == "`" else fixed_token
                next_token = tokens[i + 1] if i < len(tokens) - 1 else None

                sent.tokens.append(fixed_token)
                sent.tags.append(tag)

                # quote ends next token : skip this token
                # this avoids problem with cases where we have punctuation
                # at the end of a quote (otherwise, the end of the quote
                # would be part of the next sentence)
                if next_token == "''":
                    continue

                # sentence end
                if token in ["''", ".", "?", "!"]:
                    doc.append(sent)
                    sent = NERSentence()

            documents.append(doc)

        super().__init__(documents, **kwargs)
