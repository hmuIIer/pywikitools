import os
import re
import logging
import json
from configparser import ConfigParser
from typing import List, Optional, Dict, Tuple
import pywikibot

from pywikitools.fortraininglib import ForTrainingLib
from pywikitools.pdftools.metadata import check_metadata
from pywikitools.resourcesbot.changes import ChangeLog
from pywikitools.resourcesbot.consistency_checks import ConsistencyCheck
from pywikitools.resourcesbot.export_html import ExportHTML
from pywikitools.resourcesbot.export_repository import ExportRepository
from pywikitools.resourcesbot.write_lists import WriteList
from pywikitools.resourcesbot.data_structures import FileInfo, WorksheetInfo, LanguageInfo, \
                                                     DataStructureEncoder, json_decode
from pywikitools.resourcesbot.write_report import WriteReport
from pywikitools.resourcesbot.write_sidebar_messages import WriteSidebarMessages
from pywikitools.resourcesbot.write_summary import WriteSummary


# For short creation of a pywikibot site (new feature of pywikibot 7.3)
# TODO: use this and get rid of user-config.py
class ForTrainingFamily(pywikibot.family.Family):  # noqa: D101

    name = '4training'
    langs = {
        'en': 'www.4training.net',
    }

    def scriptpath(self, code):
        return {'en': '/mediawiki'}[code]

    def protocol(self, code):
        return {'en': 'https'}[code]


class ResourcesBot:
    """Contains all the logic of our bot"""

    def __init__(self, config: ConfigParser, limit_to_lang: Optional[str] = None, rewrite_type: str = 'rewrite-all',
                 read_from_cache: bool = False):
        """
        @param limit_to_lang: limit processing to one language (string with a language code)
        @param rewrite_type: Set rewrite type. Default is rewrite all language information regardless of changes
        @param read_from_cache: Read from json cache from the mediawiki system (don't query individual worksheets)
        """
        # read-only list of download file types
        self._file_types = ["pdf", "odt", "odg", "printPdf"]
        # Read the configuration from config.ini in the same directory
        self._config = config
        self.logger = logging.getLogger('pywikitools.resourcesbot')
        self.site: pywikibot.site.APISite = pywikibot.Site()
#       TODO use this and get rid of user-config.py
#        family = ForTrainingFamily()
#        self.site: pywikibot.site.APISite = pywikibot.Site(code='en', fam=family, user="AvailableResourcesBot")
        if not self._config.has_option('mediawiki', 'baseurl') or \
           not self._config.has_option('mediawiki', 'scriptpath'):
            raise RuntimeError("Missing settings for mediawiki connection in config.ini")
        if not self._config.has_option("Paths", "temp"):
            self.logger.warning("Missing path for temporary files in config.ini")
            self._config.set("Paths", "temp", os.path.abspath(os.getcwd()) + "/temp/")
        if not os.path.isdir(self._config.get("Paths", "temp")):
            os.makedirs(self._config.get("Paths", "temp"))

        self.fortraininglib: ForTrainingLib = ForTrainingLib(self._config.get('mediawiki', 'baseurl'),
                                                             self._config.get('mediawiki', 'scriptpath'))

        self._limit_to_lang: Optional[str] = limit_to_lang
        self._read_from_cache: bool = read_from_cache
        self._rewrite_type: str = rewrite_type
        if self._limit_to_lang is not None:
            self.logger.info(f"Parameter lang is set, limiting processing to language {limit_to_lang}")
        if self._read_from_cache:
            self.logger.info("Parameter --read-from-cache is set, reading from JSON...")
        if self._rewrite_type:
            self.logger.info(f'Parameter {rewrite_type} is set')

        # Stores details on all languages: language code -> information about all worksheets in that language
        self._result: Dict[str, LanguageInfo] = {}

        # Changes since the last run (will be filled after gathering of all information is done)
        self._changelog: Dict[str, ChangeLog] = {}

    def run(self):
        if self._read_from_cache:
            try:
                language_list: List[str] = []   # List of languages to be read from cache
                if self._limit_to_lang is None:
                    page = pywikibot.Page(self.site, "4training:languages.json")
                    if not page.exists():
                        raise RuntimeError("Couldn't load list of languages from 4training:languages.json")
                    language_list = json.loads(page.text)
                    assert isinstance(language_list, list)
                else:
                    language_list.append(self._limit_to_lang)
                    language_list.append("en")  # We need the English infos for LanguagePostProcessors

                for lang in language_list:      # Now we read the details for each language
                    self.logger.info(f"Reading details for language {lang} from cache...")
                    page = pywikibot.Page(self.site, f"4training:{lang}.json")
                    if not page.exists():
                        raise RuntimeError(f"Couldn't load from cache for language {lang}")
                    language_info = json.loads(page.text, object_hook=json_decode)
                    assert isinstance(language_info, LanguageInfo)
                    assert language_info.language_code == lang
                    self._result[lang] = language_info
            except AssertionError:
                raise RuntimeError("Unexpected error while parsing JSON data from cache.")

        else:
            self._result["en"] = LanguageInfo("en", "English")
            for worksheet in self.fortraininglib.get_worksheet_list():
                # Gather all data (this takes quite some time!)
                self._query_translations(worksheet)

        # That shouldn't be necessary but for some reasons the script sometimes failed with WARNING from pywikibot:
        # "No user is logged in on site 4training:en" -> better check and try to log in if necessary
        if not self.site.logged_in():
            self.logger.info("We're not logged in. Trying to log in...")
            self.site.login()
            if not self.site.logged_in():
                self.site.getuserinfo()
                self.logger.warning(f"userinfo: {self.site.userinfo}")
                raise RuntimeError("Login with pywikibot failed.")

        # Find out what has been changed since our last run
        for lang, language_info in self._result.items():
            self._changelog[lang] = self._sync_and_compare(language_info) if not self._read_from_cache else ChangeLog()
        if not self._read_from_cache and self._limit_to_lang is None:
            self._save_languages_list()
            self._save_number_of_languages()        # TODO move this to a GlobalPostProcessor

        # Run all LanguagePostProcessors
        write_list = WriteList(self.fortraininglib, self.site,
                               self._config.get("resourcesbot", "username", fallback=""),
                               self._config.get("resourcesbot", "password", fallback=""),
                               True if self._rewrite_type == 'all' else False)
        write_report = WriteReport(self.fortraininglib, self.site, True if self._rewrite_type == 'list' else False)
        write_sidebar_messages = WriteSidebarMessages(self.fortraininglib, self.site,
                                                      True if self._rewrite_type == 'sidebar' else False)
        consistency_check = ConsistencyCheck(self.fortraininglib)
        export_html = ExportHTML(self.fortraininglib, self._config.get("Paths", "htmlexport", fallback=""),
                                 True if self._rewrite_type == 'html' else False)
        export_repository = ExportRepository(self._config.get("Paths", "htmlexport", fallback=""))
        assert "en" in self._result
        for lang in self._result:
            consistency_check.run(self._result[lang], self._result["en"], ChangeLog())
            export_html.run(self._result[lang], self._result["en"], self._changelog[lang])
            export_repository.run(self._result[lang], self._result["en"], self._changelog[lang])
            write_list.run(self._result[lang], self._result["en"], self._changelog[lang])
            write_report.run(self._result[lang], self._result["en"], self._changelog[lang])
            write_sidebar_messages.run(self._result[lang], self._result["en"], self._changelog[lang])

        # Now run all GlobalPostProcessors
        if not self._limit_to_lang:
            write_summary = WriteSummary(self.site, self._rewrite_type)
            write_summary.run(self._result, self._changelog)

    def get_english_version(self, page_source: str) -> Tuple[str, int]:
        """
        Extract version of an English worksheet
        @return Tuple of version string and the number of the translation unit where it is stored
        """
        handler = re.search(r"\{\{Version\|<translate>*?<!--T:(\d+)-->\s*([^<]+)</translate>", page_source)
        if handler:
            return (handler.group(2), int(handler.group(1)))
        self.logger.warning("Couldn't retrieve version from English worksheet!")
        return ("", 0)

    def _query_translated_file(self, worksheet: WorksheetInfo, english_file_info: FileInfo) -> None:
        """
        Query the name of the translated file and see if it is valid. If yes, go ahead and see if such a file exists
        """
        if english_file_info.translation_unit is None:
            self.logger.warning(f"Internal error: translation unit is None in {english_file_info}, ignoring.")
            return
        file_name = self.fortraininglib.get_translated_unit(worksheet.page, worksheet.language_code,
                                                            english_file_info.translation_unit)
        warning: str = ""
        if file_name is None:
            warning = "does not exist"
        elif (file_name == '-') or (file_name == '.'):
            warning = f"is placeholder: {file_name}"
        elif file_name == english_file_info.get_file_name():
            warning = "is identical with English original"
        if warning != "":
            # TODO fill that translation unit with "-"
            if not worksheet.progress.is_unfinished():    # No need to write warnings if the translation is unfinished
                self.logger.warning(f"Warning: translation {worksheet.page}/{english_file_info.translation_unit}/"
                                    f"{worksheet.language_code} (for {english_file_info.file_type} file) {warning}")
            return
        assert file_name is not None    # Make mypy happy in the next line
        self._add_file_type(worksheet, english_file_info.file_type, file_name)

    def _add_file_type(self, worksheet: WorksheetInfo, file_type: str, file_name: str, unit: Optional[int] = None):
        """Try to add details on this translated file to worksheet - warn if it doesn't exist."""
        try:
            file_page = pywikibot.FilePage(self.site, file_name)
            if file_page.exists():
                metadata = None
                if file_type == "pdf":
                    # If it's a PDF, we try to analyze the metadata and save it also in our data structure
                    temp_file = os.path.join(self._config.get("Paths", "temp"), file_name)
                    if file_page.download(temp_file):
                        metadata = check_metadata(self.fortraininglib, temp_file, worksheet)
                        if not metadata.correct:
                            self.logger.warning(f"{file_name} metadata is incorrect: {metadata.warnings}")
                        if not metadata.pdf1a:
                            self.logger.info(f"{file_name} is not PDF/1A")
                        if metadata.only_docinfo:
                            self.logger.info(f"{file_name} uses only outdated DocInfo in PDF metadata")
                        os.remove(temp_file)
                    else:
                        self.logger.warning(f"Downloading {file_name} failed. Couldn't analyze PDF metadata")
                worksheet.add_file_info(file_type=file_type, from_pywikibot=file_page.latest_file_info,
                                        unit=unit, metadata=metadata)
            else:
                self.logger.warning(f"Page {worksheet.page}/{worksheet.language_code}: Couldn't find {file_name}.")
        except pywikibot.exceptions.Error as err:
            self.logger.warning(f"Exception thrown for {file_type} file: {err}")

    def _add_english_file_infos(self, page_source: str, worksheet: WorksheetInfo) -> None:
        """
        Finds out the names of the English downloadable files (originals)
        and adds them to worksheet
        """
        for file_type in self._file_types:
            handler = re.search(r"\{\{" + file_type[0].upper() + file_type[1:] +
                                r"Download\|<translate>*?<!--T:(\d+)-->\s*([^<]+)</translate>", page_source)
            if handler:
                self._add_file_type(worksheet, file_type, handler.group(2), int(handler.group(1)))

    def _query_translations(self, page: str):
        """
        Go through one worksheet, check all existing translations and gather information into self._result
        @param: page: Name of the worksheet
        """
        # This is querying more data than necessary when self._limit_to_lang is set. But to save time we'd need to find
        # a different API call that is only requesting progress for one particular language... for now it's okay
        available_translations = self.fortraininglib.list_page_translations(page, include_unfinished=True)
        english_title = self.fortraininglib.get_translated_title(page, "en")
        page_source = self.fortraininglib.get_page_source(page)
        if english_title is None or page_source is None:
            self.logger.error(f"Couldn't get English page {page}, skipping.")
            return
        version, version_unit = self.get_english_version(page_source)
        english_page_info: WorksheetInfo = WorksheetInfo(page, "en", english_title, available_translations["en"],
                                                         version, version_unit)
        self._add_english_file_infos(page_source, english_page_info)
        self._result["en"].add_worksheet_info(page, english_page_info)

        finished_translations = []
        for lang, progress in available_translations.items():
            if (self._limit_to_lang is not None) and (self._limit_to_lang != lang):
                continue
            if lang == "en":    # We saved information on the English originals already, don't do that again
                continue

            translated_title = self.fortraininglib.get_translated_title(page, lang)
            if translated_title is None:  # apparently this translation doesn't exist
                if not progress.is_unfinished():
                    self.logger.warning(f"Language {lang}: Title of {page} not translated, skipping.")
                continue
            translated_version = self.fortraininglib.get_translated_unit(page, lang, version_unit)
            if translated_version is None:
                if not progress.is_unfinished():
                    self.logger.warning(f"Language {lang}: Version of {page} not translated, skipping.")
                continue

            if progress.is_unfinished():
                self.logger.info(f"Ignoring translation {page}/{lang} - ({progress} translation units translated)")
            else:
                finished_translations.append(lang)
            page_info = WorksheetInfo(page, lang, translated_title, progress, translated_version)
            if not page_info.has_same_version(english_page_info):
                self.logger.warning(f"Language {lang}: {translated_title} has version {translated_version}"
                                    f" - {english_title} has version {version}")

            for file_info in english_page_info.get_file_infos().values():
                self._query_translated_file(page_info, file_info)

            if lang not in self._result:
                language_name = self.fortraininglib.get_language_name(lang, 'en') or ""
                self._result[lang] = LanguageInfo(lang, language_name)
            self._result[lang].add_worksheet_info(page, page_info)

        self.logger.info(f"Worksheet {page} is translated into: {finished_translations}, "
                         f"ignored {set(available_translations.keys()) - set(finished_translations)}")

    def _sync_and_compare(self, language_info: LanguageInfo) -> ChangeLog:
        """
        Synchronize our generated data on this language with our "database" and return the changes.

        The "database" is the JSON representation of LanguageInfo and is stored in a mediawiki page.

        @param lang language code
        @return comparison to what was previously stored in our database
        """
        lang = language_info.language_code
        encoded_json = DataStructureEncoder().encode(language_info)
        old_language_info: LanguageInfo = LanguageInfo(lang, language_info.english_name)
        rewrite_json: bool = self._rewrite_type

        # Reading data structure from our mediawiki, stored in e.g. https://www.4training.net/4training:de.json
        page = pywikibot.Page(self.site, f"4training:{lang}.json")
        if not page.exists():
            # There doesn't seem to be any information on this language stored yet!
            self.logger.warning(f"{page.full_url()} doesn't seem to exist yet. Creating...")
            page.text = encoded_json
            page.save("Created JSON data structure")
            rewrite_json = False
        else:
            # Load "old" data structure of this language (from previous resourcesbot run)
            try:
                old_language_info = json.loads(page.text, object_hook=json_decode)
                assert isinstance(old_language_info, LanguageInfo)
                assert old_language_info.language_code == lang
            except AssertionError:
                self.logger.warning(f"Error while trying to load {lang}.json")

            if encoded_json != page.text:
                rewrite_json = True

        # compare and find out if new worksheets have been added
        changes: ChangeLog = language_info.compare(old_language_info)
        if changes.is_empty():
            self.logger.info(f"No changes in language {lang} since last run.")
        else:
            self.logger.info(f"Changes in language {lang} since last run:\n{changes}")

        if rewrite_json:
            # Write the updated JSON structure
            page.text = encoded_json
            page.save("Updated JSON data structure")
            self.logger.info(f"Updated 4training:{lang}.json")

        return changes

    def _save_languages_list(self):
        """
        Save a list of language codes of all our languages to the mediawiki server
        We want this list so that the bot can be run with --read-from-cache for all languages

        The list is stored to https://www.4training.net/4training:languages.json in alphabetical order
        """
        language_list = list(self._result)
        language_list.sort()
        encoded_json: str = json.dumps(language_list)
        previous_json: str = ""

        page = pywikibot.Page(self.site, "4training:languages.json")
        if not page.exists():
            self.logger.warning("languages.json doesn't seem to exist yet. Creating...")
        else:
            previous_json = page.text

        # TODO compare language_list and json.loads(previous_json) to find out if a new language was added
        if previous_json != encoded_json:
            page.text = encoded_json
            page.save("Updated list of languages")
            self.logger.info("Updated 4training:languages.json")

    def _save_number_of_languages(self):
        """
        Count number of languages we have and save them to https://www.4training.net/MediaWiki:Numberoflanguages
        Language variants (any language code containing a "-") are not counted extra.
        TODO: Discuss how we want to count in some edge cases, e.g. count pt-br always extra as we have a
        separate page for Brazilian Portuguese?
        @param language_list: List of language codes
        """
        language_list: List[str] = list(self._result)
        number_of_languages = 0
        for lang in language_list:
            if "-" not in lang:
                number_of_languages += 1
            else:
                self.logger.debug(f"Not counting {lang} into the number of languages we have")
        self.logger.info(f"Number of languages: {number_of_languages}")

        previous_number_of_languages: int = 0
        page = pywikibot.Page(self.site, "MediaWiki:Numberoflanguages")
        if page.exists():
            previous_number_of_languages = int(page.text)
        else:
            self.logger.warning("MediaWiki:Numberoflanguages doesn't seem to exist yet. Creating...")

        if previous_number_of_languages != number_of_languages:
            try:
                page.text = number_of_languages
                page.save("Updated number of languages")
                self.logger.info(f"Updated MediaWiki:Numberoflanguages to {number_of_languages}")
            except pywikibot.exceptions.PageSaveRelatedError as err:
                self.logger.warning(f"Error while trying to update MediaWiki:Numberoflanguages: {err}")
