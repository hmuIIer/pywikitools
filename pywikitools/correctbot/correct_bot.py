#!/usr/bin/python3
# -*- coding: utf-8 -*-
"""
Bot that replaces common typos for different languages.
This requires the pywikibot framework.

Documentation should also go to https://www.4training.net/User:TheCorrectBot

Run with dummy page with available translation units
www.4training.net/mediawiki/api.php?action=query&list=messagecollection&mcgroup=page-CorrectTestpage&mclanguage=fr
"""

import argparse
import logging
import importlib
import sys
from typing import Callable, List, Optional

from communicator import PageWrapper
from communicator import Communicator
from pywikitools import fortraininglib
from pywikitools.lang.translated_page import TranslationUnit

class CorrectBot:
    def __init__(self, simulate: bool = False, loglevel: Optional[str] = None):
        self.logger = logging.getLogger("pywikitools.correctbot")
        self._simulate: bool = simulate
        self._diff: str = ""
        self._stats: Optional[str] = None
        self._correction_counter: int = 0

        if loglevel is not None:
            numeric_level = getattr(logging, loglevel.upper(), None)
            if not isinstance(numeric_level, int):
                raise ValueError(f"Invalid log level: {loglevel}")
            logging.basicConfig(level=numeric_level)
            self.logger.setLevel(numeric_level)

    def load_corrector(self, language_code: str) -> Callable:
        """Load the corrector class and return it. Exit on error"""
        # Dynamically load e.g. correctors/de.py
        module_name = f"correctors.{language_code}"
        module = importlib.import_module(module_name, ".")
        # There should be exactly one class named "XYCorrector" in there - let's get it
        for class_name in dir(module):
            if "Corrector" in class_name:
                corrector_class = getattr(module, class_name)
                # Filter out CorrectorBase (in module correctors.base) and classes from correctors.universal
                if corrector_class.__module__ == module_name:
                    return corrector_class

        logging.fatal(f"Couldn't load corrector for language {language_code}. Giving up")
        sys.exit(1)

    def check_page(self, page: str, language_code: str):
        translation_units: List[TranslationUnit] = fortraininglib.get_translation_units(page, language_code)
        corrector = self.load_corrector(language_code)()
        self._diff = ""
        for translation_unit in translation_units:
            if translation_unit.is_translation_well_structured():
                for _, snippet in translation_unit:
                    snippet.content = corrector.correct(snippet.content)
                translation_unit.sync_from_snippets()
            else:
                self.logger.warning(f"{translation_unit.get_name()} is not well structured.")
                translation_unit.set_translation(corrector.correct(translation_unit.get_translation()))

            diff = translation_unit.get_translation_diff()
            if diff != "":
                self._diff += f"{translation_unit.get_name()}: {diff}\n"
        self._stats = corrector.print_stats()
        self._correction_counter = corrector.count_corrections()

    def get_stats(self) -> Optional[str]:
        return self._stats

    def get_correction_counter(self) -> int:
        return self._correction_counter

    def get_diff(self) -> str:
        return self._diff

    def run(self, page: str, language_code: str):
        """
        Correct the translation of a page.
        TODO write it back to the system if we're not in simulation mode
        """
        self.check_page(page, language_code)
        print(self.get_diff())
        print(self.get_stats())

        # TODO save changes back to mediawiki system


def parse_arguments() -> argparse.Namespace:
    """
    Parses the arguments given from outside

    Returns:
        argparse.Namespace: parsed arguments
    """
    log_levels: List[str] = ['debug', 'info', 'warning', 'error']

    parser = argparse.ArgumentParser()
    parser.add_argument("page", help="Name of the mediawiki page")
    parser.add_argument("language_code", help="Language code")
    parser.add_argument("-s", "--simulate", type=bool, default=False, required=False,
                        help="Simulates the corrections but does not apply them to the webpage.")
    parser.add_argument("-l", "--loglevel", choices=log_levels, help="set loglevel for the script")
    return parser.parse_args()


if __name__ == "__main__":
    args = parse_arguments()
    correctbot = CorrectBot(args.simulate, args.loglevel)
    correctbot.run(args.page, args.language_code)

