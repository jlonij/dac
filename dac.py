#!/usr/bin/env python
# -*- coding: utf-8 -*-
#
# DAC Entity Linker
#
# Copyright (C) 2017 Koninklijke Bibliotheek, National Library of
# the Netherlands
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE. See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program. If not, see <http://www.gnu.org/licenses/>.

import config
import dictionary
import json
import Levenshtein
import math
import models
import numpy as np
import os
import re
import requests
import scipy
import solr
import sys
import utilities

from lxml import etree
from operator import attrgetter
from sklearn.metrics.pairwise import cosine_similarity

conf = config.parse_config(local=True)

JSRU_URL = conf.get("JSRU_URL")
TPTA_URL = conf.get("TPTA_URL")
SOLR_URL = conf.get("SOLR_URL")
W2V_URL = conf.get("W2V_URL")
SOLR_ROWS = conf.get("SOLR_ROWS")
MIN_PROB = conf.get("MIN_PROB")


class EntityLinker():
    '''
    Link named entity mention(s) in an article to a DBpedia description.
    '''

    def __init__(self, tpta_url=None, solr_url=None, debug=False, train=False,
            features=False, candidates=False, model=None):
        '''
        Initialize the disambiguation model and Solr connection.
        '''
        self.tpta_url = tpta_url if tpta_url else TPTA_URL
        self.solr_url = solr_url if solr_url else SOLR_URL

        self.solr_connection = solr.SolrConnection(self.solr_url)

        self.debug = debug
        self.train = train
        self.features = features
        self.candidates = candidates

        if train:
            self.model = models.Model()
        elif model == 'svm':
            self.model = models.LinearSVM()
        elif model == 'nn':
            self.model = models.NeuralNet()
        elif model == 'bnn':
            self.model = models.BranchingNeuralNet()
        else:
            self.model = models.NeuralNet()

    def link(self, url, ne=None):
        '''
        Link named entity mention(s) in an article to a DBpedia description.
        '''
        # Get context information (article ocr, metadata etc.)
        try:
            self.context = Context(url, self.tpta_url)
        except Exception as e:
            if self.debug:
                raise
            return {'status': 'error', 'message': 'Error retrieving context: '
                    + str(e)}

        # If a specific ne was requested, search for a corresponding entity in
        # the list of recognized entities
        if ne:
            ne = ne.decode('utf-8')
            entity_to_link = None
            for entity in self.context.entities:
                if ne == entity.text:
                    entity_to_link = entity
            # If not found, create new one
            if not entity_to_link:
                entity_to_link = Entity(ne, None, self.context)
                self.context.entities.append(entity_to_link)

        # Group related entities into clusters
        clusters_to_link = self.get_clusters(self.context.entities)
        if ne:
            # Link only the cluster to which the entity belongs
            clusters_to_link = [c for c in clusters_to_link if entity_to_link
                in c.entities]

        # Process all clusters to be linked
        clusters_linked = []

        while clusters_to_link:
            cluster = clusters_to_link.pop()
            try:
                result = cluster.link(self.solr_connection, SOLR_ROWS,
                    self.model, MIN_PROB, self.train)
            except Exception as e:
                if self.debug:
                    raise
                return {'status': 'error', 'message': 'Error linking entity: '
                        + str(e)}

            # If a cluster consists of multiple entities and could not be linked
            # or was not linked to a person, split it up and return the parts to
            # the queue. If not, add the cluster to the linked list.
            dependencies = [e for e in cluster.entities if e.norm !=
                cluster.entities[0].norm]

            if dependencies:
                types = []
                if result.description:
                    if result.description.document.get('schema_type'):
                        types += result.description.document.get('schema_type')
                    if result.description.document.get('dbo_type'):
                        types += result.description.document.get('dbo_type')
                if not result.description or 'Person' not in types:
                    new_clusters = [Cluster([e for e in cluster.entities if e
                        not in dependencies])]
                    new_clusters.extend(self.get_clusters(dependencies))

                    # If linking a specific ne, only return the new cluster
                    # containing that ne to the queue
                    if ne:
                        clusters_to_link.extend([c for c in new_clusters if
                            entity_to_link in c.entities])
                    else:
                        clusters_to_link.extend(new_clusters)
                else:
                    clusters_linked.append(cluster)
            else:
                clusters_linked.append(cluster)

        # Return the result for each (unique) entity
        results = []
        to_return = [entity_to_link] if ne else self.context.entities
        for entity in to_return:
            if entity.text not in [result['text'] for result in results]:
                for cluster in clusters_linked:
                    if entity in cluster.entities:
                        result = cluster.result.get_dict(features=self.features,
                            candidates=self.candidates)
                        result['text'] = entity.text
                        if self.debug or 'link' in result:
                            results.append(result)

        return {'status': 'ok', 'linkedNEs': results}

    def get_clusters(self, entities):
        '''
        Group related entities into clusters.
        '''
        clusters = []
        # Arrange the entities in reversed alphabetical order
        sorted_entities = sorted(entities, key=attrgetter('norm'), reverse=True)
        # Arrange the entities by word length, longest first
        sorted_entities = sorted(sorted_entities, key=lambda entity:
            len(entity.norm.split()), reverse=True)
        # Assign each entity to a cluster
        for entity in sorted_entities:
            clusters = self.cluster(entity, clusters)
        return clusters

    def cluster(self, entity, clusters):
        '''
        Either add entity to an existing cluster or create a new one.
        '''
        # If the entity text or norm exactly matches an existing cluster,
        # add it to the cluster
        for cluster in clusters:
            for e in cluster.entities:
                if entity.text == e.text:
                    cluster.entities.append(entity)
                    return clusters
                if len(entity.norm) > 0 and len(e.norm) > 0:
                    if entity.norm == e.norm:
                        cluster.entities.append(entity)
                        return clusters

        # Find candidate clusters that partially match an entity
        candidates = []
        for cluster in clusters:
            for e in cluster.entities:
                if len(entity.norm) > 0 and len(e.norm) > 0:
                    # Last parts are the same
                    if entity.norm.split()[-1] == e.norm.split()[-1]:
                        # Any preceding parts are the same
                        if e.norm.endswith(entity.norm):
                            # The candidate norm is longer than the entity norm
                            if len(e.norm.split()) > len(entity.norm.split()):
                                candidates.append(cluster)
                                break
                    # First parts are the same
                    elif entity.norm.split()[0] == e.norm.split()[0]:
                        # Entity norm consists of exactly one word (first name)
                        if len(entity.norm.split()) == 1:
                            # The candidate norm is longer than the entity norm
                            if len(e.norm.split()) > len(entity.norm.split()):
                                # Both entities are probably persons
                                if e.tpta_type == 'person':
                                    if entity.tpta_type == 'person':
                                        candidates.append(cluster)
                                        break

        if len(candidates) == 1:
            candidates[0].entities.append(entity)
        else:
            clusters.append(Cluster([entity]))
        return clusters


class Context():
    '''
    The context information for an entity.
    '''

    def __init__(self, url, tpta_url):
        '''
        Retrieve ocr, metadata, subjects and entities.
        '''
        self.url = url

        self.ocr = self.get_ocr(url)
        self.entities = self.get_entities(url, tpta_url)

    def get_ocr(self, url):
        '''
        Retrieve ocr from resolver url.
        '''
        response = requests.get(url, timeout=30)
        assert response.status_code == 200, 'Error retrieving OCR'

        xml = etree.fromstring(response.content)
        ocr = etree.tostring(xml, encoding='utf8',
            method='text').decode('utf-8')
        ocr = ' '.join(ocr.split())

        return ocr

    def get_entities(self, url, tpta_url):
        '''
        Retrieve entities from the NER service and instantiate an
        entity object for each one.
        '''
        payload = {}
        payload['lang'] = 'nl'
        payload['url'] = url

        response = requests.get(tpta_url, params=payload, timeout=30)
        assert response.status_code == 200, 'TPTA error'

        xml = etree.fromstring(response.content)
        error_node = xml.find('error')
        if error_node is not None:
            raise Exception('TPTA error: ' + error_node.text)

        entities = []

        entity_nodes = xml.find('entities')
        if entity_nodes is not None:
            # Keep track of the position of each entity in the document so that
            # entity mentions with identical surface forms can be kept apart
            doc_pos = 0
            for node in entity_nodes:
                if node.text and len(node.text) > 1:
                    if isinstance(node.text, str):
                        t = node.text.decode('utf-8')
                    else:
                        t = node.text
                    entity = Entity(t, node.tag, self, doc_pos)
                    doc_pos = entity.end_pos if entity.end_pos > -1 else doc_pos
                    entities.append(entity)

        return entities

    def get_publ_year(self):
        '''
        Retrieve metadata (currently just publication date) with sru.
        '''
        payload = {}
        payload['operation'] = 'searchRetrieve'
        payload['x-collection'] = 'DDD_artikel'
        payload['query'] = 'uniqueKey=' + self.url[self.url.find('ddd:'):-4]

        response = requests.get(JSRU_URL, params=payload, timeout=30)
        assert response.status_code == 200, 'Error retrieving metadata'

        xml = etree.fromstring(response.content)

        path = '{http://www.loc.gov/zing/srw/}records/'
        path += '{http://www.loc.gov/zing/srw/}record/'
        path += '{http://www.loc.gov/zing/srw/}recordData/'
        path += '{http://purl.org/dc/elements/1.1/}date'

        date_element = xml.find(path)
        if date_element is not None:
            self.publ_year = int(date_element.text[:4])
        else:
            self.publ_year = None

    def get_subjects(self):
        '''
        Extract subjects from ocr (based on dictionary for now).
        '''
        subjects = []
        for subject in dictionary.subjects:
            words = dictionary.subjects[subject]
            for role in dictionary.roles:
                if subject in dictionary.roles[role]['subjects']:
                    words += dictionary.roles[role]['words']
            window = [utilities.normalize(w) for w in
                utilities.tokenize(self.ocr)]
            if len(set(words) & set(window)) > 0:
                subjects.append(subject)

        self.subjects = subjects

    def normalize_ocr(self):
        self.ocr_norm = utilities.normalize(self.ocr)

    def tokenize_ocr(self):
        self.ocr_bow = [w for t in utilities.tokenize(self.ocr) for w in
            utilities.normalize(t).split()]
        self.ocr_bow = list(set(self.ocr_bow))


class Entity():
    '''
    An entity mention occuring in an article.
    '''

    def __init__(self, text, tpta_type=None, context=None, doc_pos=0):
        '''
        Gather information about the entity and its immediate surroundings.
        '''
        self.text = text
        self.tpta_type = tpta_type
        self.context = context
        self.doc_pos = doc_pos

        self.norm = utilities.normalize(self.text)

        self.start_pos, self.end_pos = self.get_position(self.text,
            self.context.ocr, self.doc_pos)
        self.window_left, self.window_right = self.get_window(self.context.ocr,
            start_pos=self.start_pos, end_pos=self.end_pos, size=20)

        self.quotes = self.get_quotes()

        self.title, self.title_form = self.get_title()
        self.role, self.role_form = self.get_role()

        self.stripped = self.strip_titles()
        self.last_part = utilities.get_last_part(self.stripped)
        self.valid = self.is_valid()

        self.alt_type = self.get_alt_type()

    def get_position(self, phrase, document, doc_pos=None):
        '''
        Find the start and end position of the mention in the article.
        '''
        start_pos = document.find(phrase, doc_pos)
        end_pos = start_pos + len(phrase)
        if start_pos >= 0 and end_pos <= len(document):
            return start_pos, end_pos
        else:
            return -1, -1

    def get_window(self, document, start_pos=None, end_pos=None, size=None):
        '''
        Get the words appearing to the left and right of the entity.
        '''
        left_bow = []
        right_bow = []

        if start_pos >= 0 and end_pos <= len(document):
            left_space_pos = document.rfind(' ', 0, start_pos)
            left_new_line_pos = document.rfind('\n', 0, start_pos)
            left_pos = max([left_space_pos, left_new_line_pos])
            if left_pos > 0:
                left_bow = utilities.tokenize(document[:left_pos])
            right_space_pos = document.find(' ', end_pos)
            right_new_line_pos = document.find('\n', end_pos)
            right_pos = min([right_space_pos, right_new_line_pos])
            if right_pos > 0:
                right_bow = utilities.tokenize(document[right_space_pos:])

        if size:
            left_bow = left_bow[-size:]
            right_bow = right_bow[:size]

        return left_bow, right_bow

    def get_quotes(self):
        '''
        Count quote characters surrounding the entity.
        '''
        quotes = 0
        quote_chars = [u'"', u"'", u'„', u'”', u'‚', u'’']
        for pos in [self.start_pos - 1, self.start_pos, self.end_pos - 1,
                self.end_pos]:
            if pos >= 0 and pos < len(self.context.ocr):
                if self.context.ocr[pos] in quote_chars:
                    quotes += 1
        return quotes

    def get_title(self):
        '''
        Check for titles near the beginning of the entity.
        '''
        words = [self.norm.split()[0]]
        if self.window_left:
            words.append(utilities.normalize(self.window_left[-1]))
        for word in words:
            if word in dictionary.titles:
                return True, word
        return None, None

    def get_role(self):
        '''
        Check for roles near the beginning and end of the entity.
        '''
        words = [self.norm.split()[0]]
        if self.window_left:
            words.append(utilities.normalize(self.window_left[-1]))
        if self.window_right and self.context.ocr[self.end_pos] == ',':
            words.append(utilities.normalize(self.window_right[0]))
        for word in words:
            for role in dictionary.roles:
                if word in dictionary.roles[role]['words']:
                    return role, word
        return None, None

    def strip_titles(self):
        '''
        Remove titles and roles appearing inside the entity.
        '''
        if self.title and self.norm.split()[0] == self.title_form:
            return ' '.join(self.norm.split()[1:])
        if self.role and self.norm.split()[0] == self.role_form:
            return ' '.join(self.norm.split()[1:])
        return self.norm

    def is_valid(self):
        '''
        Check entity validity.
        '''
        if [w for w in self.stripped.split() if len(w) >= 2]:
            if self.last_part and not self.is_date():
                return True
        return False

    def is_date(self):
        '''
        Check if the entity is some sort of date.
        '''
        if [w for w in self.norm.split() if w in dictionary.months]:
            if [w for w in self.norm.split() if w.isdigit()]:
                return True
        return False

    def get_alt_type(self):
        '''
        Infer addtional information about entity type from context.
        '''
        if self.title:
            return 'person'

        if self.role:
            if len(dictionary.roles[self.role]['types']) == 1:
                return dictionary.roles[self.role]['types'][0]

        if self.window_left:
            prev_word = utilities.normalize(self.window_left[-1])
            if prev_word in ['in', 'te', 'uit']:
                return 'location'

        return None

    def substitute(self):
        '''
        Try to substitute norm with basic spelling variant.
        '''
        subs = []

        # Replace y with ij
        if self.stripped.find('y') > -1:
            subs.append(self.stripped.replace('y', 'ij'))

        # Remove trailing s
        if self.stripped.endswith('s'):
            subs.append(self.stripped[:-1])

        # Replace sch(e) with s(e)
        pattern = r'(^|\s)([a-zA-Z]{2,})sch(e?)($|\s)'
        if re.search(pattern, self.stripped):
            subs.append(re.sub(pattern, r'\1\2s\3\4', self.stripped))

        # Replace trailing v with w
        pattern = r'(^|\s)([a-zA-Z]{2,})v($|\s)'
        if re.search(pattern, self.stripped):
            subs.append(re.sub(pattern, r'\1\2w\3', self.stripped))

        # Replace trailing w with v
        pattern = r'(^|\s)([a-zA-Z]{2,})w($|\s)'
        if re.search(pattern, self.stripped):
            subs.append(re.sub(pattern, r'\1\2v\3', self.stripped))

        # If there is exactly one possible substitution, replace norm, stripped
        # and last_part
        if len(subs) == 1:
            self.norm = self.norm.replace(self.stripped, subs[0])
            self.stripped = subs[0]
            self.last_part = utilities.get_last_part(self.stripped)
            return True

        return False


class Cluster():
    '''
    Group of related entity mentions, presumed to refer to the same entity.
    '''

    def __init__(self, entities):
        '''
        Initialize cluster.
        '''
        self.entities = entities
        self.context = self.entities[0].context

    def link(self, solr_connection, solr_rows, model, min_prob, train):
        '''
        Get the link result for the cluster.
        '''
        # Check validity of the main entity
        if not self.entities[0].valid:
            self.result = Result("Invalid entity")
            return self.result

        # If entity is valid, try to query Solr for candidate descriptions
        cand_list = CandidateList(solr_connection, solr_rows, self, model)

        # Check the number of descriptions found
        if len(cand_list.candidates) == 0:
            self.result = Result("Nothing found")
            return self.result

        # Filter descriptions according to hard criteria, e.g. name conlfict
        cand_list.filter()
        if len(cand_list.filtered_candidates) == 0:
            self.result = Result("Name or date conflict", cand_list=cand_list)
            return self.result

        # If any candidates remain, calculate probabilities and select the best
        cand_list.rank(train)
        best_match = cand_list.ranked_candidates[0]
        if best_match.prob >= min_prob:
            self.result = Result("Predicted link", best_match.prob, best_match,
                cand_list=cand_list)
        else:
            self.result = Result("Probability too low for: " +
                best_match.document.get('label'), best_match.prob, best_match,
                cand_list=cand_list)
        return self.result

    def get_type_ratios(self):
        '''
        Get the type ratios for the cluster.
        '''
        types = [e.tpta_type for e in self.entities if e.tpta_type]
        types += [e.alt_type for e in self.entities if e.alt_type]

        if types:
            type_ratios = {}
            for t in list(set(types[:])):
                type_ratios[t] = types.count(t) / float(len(types))
        else:
            type_ratios = None

        self.type_ratios = type_ratios

    def get_window(self):
        '''
        Get combined window of all cluster entities, excluding entity parts.
        '''
        if not hasattr(self, 'entity_parts'):
            self.get_entity_parts()

        entity_parts = self.entity_parts

        window = []
        for e in self.entities:
            for w in e.window_left + e.window_right:
                norm = utilities.normalize(w)
                window.extend(norm.split())
            if e.title:
                window.append(e.title_form)
            if e.role:
                window.append(e.role_form)

        window = [w for w in window if len(w) > 4 and w not in entity_parts
            and w not in dictionary.unwanted]

        self.window = window

    def get_entity_parts(self):
        self.entity_parts = list(set([p for e in self.entities for p in
            e.stripped.split()]))

    def get_context_entity_parts(self):
        if not hasattr(self, 'entity_parts'):
            self.get_entity_parts()

        context_entity_parts = [p for e in self.context.entities for p in
            e.norm.split() if p not in self.entity_parts and p not in
            dictionary.unwanted and len(p) > 4 and e.valid]
        self.context_entity_parts = list(set(context_entity_parts))


class CandidateList():
    '''
    List of candidate links for an entity cluster.
    '''

    def __init__(self, solr_connection, solr_rows, cluster, model):
        '''
        Query the Solr index and generate initial list of candidates.
        '''
        self.solr_connection = solr_connection
        self.solr_rows = solr_rows
        self.model = model
        self.cluster = cluster

        candidates = []

        for i in range(2):
            if candidates:
                break
            if i == 1:
                if not self.cluster.entities[0].substitute():
                    break

            norm = self.cluster.entities[0].norm
            stripped = self.cluster.entities[0].stripped
            last_part = self.cluster.entities[0].last_part

            queries = []
            queries.append('pref_label_str:"' + norm + '" OR pref_label_str:"' +
                stripped + '"')
            queries.append('alt_label_str:"' + norm + '" OR alt_label_str:"' +
                stripped + '"')
            queries.append('pref_label:"' + norm + '" OR pref_label:"' +
                stripped + '"')
            queries.append('last_part_str:"' + last_part + '"')
            self.queries = queries

            for query_id, query in enumerate(queries):
                if not len(candidates) < solr_rows:
                    break
                else:
                    rows = solr_rows - len(candidates)

                solr_response = solr_connection.query(q=query, rows=rows,
                    indent='on', sort='lang,inlinks', sort_order='desc')

                for r in solr_response.results:
                    if r.get('id') not in [c.document.get('id') for c in
                            candidates]:
                        candidates.append(Description(r, i, query_id, self,
                            cluster))

        self.candidates = candidates

    def filter(self):
        '''
        Filter descriptions according to hard criteria, e.g. name conflict.
        '''
        self.filtered_candidates = []
        for c in self.candidates:
            c.set_rule_features()
            if c.match_str_conflict == 0 and c.match_txt_date > -1:
                self.filtered_candidates.append(c)

    def rank(self, train):
        '''
        Rank candidates according to trained model. Only calculate features
        values in training mode.
        '''
        for c in self.filtered_candidates:
            c.set_prob_features()
            if not train:
                example = []
                for j in range(len(self.model.features)):
                    example.append(float(getattr(c, self.model.features[j])))
                c.prob = self.model.predict(example)

        self.ranked_candidates = sorted(self.filtered_candidates,
            key=attrgetter('prob'), reverse=True)

    def set_max_score(self):
        '''
        Set the maximum Solr score of the filtered candidates.
        '''
        self.max_score = max([c.document.get('score') for c in
            self.filtered_candidates])

    def set_sum_inlinks(self):
        '''
        Set the sum of inlinks and inlinks_newspapers for the filtered
        candidates.
        '''
        for link_type in ['inlinks', 'inlinks_newspapers']:
            link_sum = sum([c.document.get(link_type) for c in
                self.filtered_candidates if c.document.get(link_type)])
            setattr(self, 'sum_' + link_type, link_sum)


class Description():
    '''
    Description of a link candidate.
    '''
    def __init__(self, document, query_iteration, query_id, cand_list, cluster):
        '''
        Initialize description.
        '''
        self.document = document
        self.query_iteration = query_iteration
        self.query_id = query_id
        self.cand_list = cand_list
        self.cluster = cluster
        self.prob = 0.0

        self.features = self.cand_list.model.features

        for f in self.features:
            setattr(self, f, 0)

    def set_rule_features(self):
        '''
        Set the feature values needed for rule-based candidate filtering.
        '''
        # Date conflict
        if self.set_date_match() > -1:

            # Name conflict
            self.set_pref_label_match()
            self.set_alt_label_match()
            self.set_last_part_match()
            self.set_first_part_match()
            self.set_non_matching()
            self.set_name_conflict()

    def set_date_match(self):
        '''
        Compare publication year of the article with birth and death years in
        the entity description.
        '''
        if not hasattr(self.cluster.context, 'publ_year'):
            self.cluster.context.get_publ_year()

        publ_year = self.cluster.context.publ_year
        if not publ_year:
            return 0

        birth_year = self.document.get('birth_year')
        death_year = self.document.get('death_year')
        if not birth_year:
            return 0
        if not death_year:
            death_year = birth_year + 80

        if publ_year < birth_year:
            self.match_txt_date = -1
        elif publ_year < birth_year + 20:
            self.match_txt_date = 0.5
        elif publ_year < death_year + 20:
            self.match_txt_date = 1
        else:
            self.match_txt_date = 0.75

        return self.match_txt_date

    def set_pref_label_match(self):
        '''
        Match the main description label with the normalized entity.
        '''
        self.non_matching = []

        label = self.document.get('pref_label')
        ne = self.cluster.entities[0].norm

        if len(set(ne.split()) - set(label.split())) == 0:
            if label == ne:
                self.match_str_pref_label_exact = 1
            elif label.endswith(ne):
                self.match_str_pref_label_end = 1
            elif label.find(ne) > -1:
                self.match_str_pref_label = 1
            else:
                self.non_matching.append(label)
        else:
            self.non_matching.append(label)

    def set_alt_label_match(self):
        '''
        Match alternative labels with the normalized entity.
        '''
        labels = self.document.get('alt_label')
        if not labels:
            return

        ne = self.cluster.entities[0].norm

        alt_label_exact_match = 0
        alt_label_end_match = 0
        alt_label_match = 0
        for l in labels:
            if len(set(ne.split()) - set(l.split())) == 0:
                if l == ne:
                    alt_label_exact_match += 1
                elif l.endswith(ne):
                    alt_label_end_match += 1
                elif l.find(ne) > -1:
                    alt_label_match += 1
                else:
                    self.non_matching.append(l)
            else:
                self.non_matching.append(l)

        self.match_str_alt_label_exact = math.tanh(alt_label_exact_match * 0.25)
        self.match_str_alt_label_end = math.tanh(alt_label_end_match * 0.25)
        self.match_str_alt_label = math.tanh(alt_label_match * 0.25)

    def set_last_part_match(self):
        '''
        Match the last part of the stripped entity with all labels,
        making sure preceding parts (e.g. initials) don't conflict.
        '''
        labels = self.non_matching
        if not labels:
            return

        ne = self.cluster.entities[0].stripped
        #if len(ne.split()) == 1:
        #    self.non_matching_labels = min(len(self.non_matching), 5) / 5.0
        #    return

        last_part_match = 0
        for l in labels[:]:

            if len(ne.split()) > len(l.split()):
                continue

            # If the last words of the title and the ne match approximately,
            # i.e. edit distance does not exceed 1
            if Levenshtein.distance(ne.split()[-1], l.split()[-1]) <= 1:

                # Multi-word entities: check for conflicts among preceding parts
                conflict = False
                source = ne.split()
                target = l.split()

                target_pos = 0
                for part in source[:-1]:
                    if target_pos < len(target[:-1]):

                        # Full words
                        if len(part) > 1 and part in target[target_pos:-1]:
                            target_pos = target.index(part) + 1

                        # First names may differ with one character
                        elif len(part) > 1 and len([p for p in
                                target[target_pos:-1] if Levenshtein.distance(p,
                                part) == 1]) > 0:
                            for p in target[target_pos:-1]:
                                if Levenshtein.distance(p, part) == 1:
                                    target_pos = target.index(p) + 1
                                    break

                        # Initials
                        elif len(part) <= 1 and part[0] in [p[0] for p in
                                target[target_pos:-1]]:
                            target_pos = [p[0] for p in
                                target[target_pos:-1]].index(part[0]) + 1

                        else:
                            conflict = True
                            break
                    else:
                        conflict = True
                        break

                if not conflict:
                    last_part_match += 1
                    self.non_matching.remove(l)

        self.match_str_last_part = math.tanh(last_part_match * 0.25)

    def set_first_part_match(self):
        ne = self.cluster.entities[0].norm
        if len(ne.split()) > 1:
            return

        last_part = self.document.get('last_part')
        if not last_part:
            return

        labels = self.non_matching
        labels = [l for l in labels if len(l.split()) > 1 and
            l.split()[0] == ne]
        if not labels:
            return

        if not hasattr(self.cluster.context, 'ocr_norm'):
            self.cluster.context.normalize_ocr()
        ocr = self.cluster.context.ocr_norm

        self.match_str_first_part = -1
        for l in labels[:]:
            if ocr.find(' '.join(l.split()[1:])) > -1:
                self.match_str_first_part = 1
                self.non_matching.remove(l)

    def set_non_matching(self):
        self.match_str_non_matching = math.tanh(len(self.non_matching) * 0.25)

    def set_name_conflict(self):
        '''
        Determine if the description has a name conflict, i.e. not a single
        sufficiently matching label was found.
        '''
        features = ['match_str_pref_label_exact', 'match_str_pref_label_end',
            'match_str_alt_label_exact', 'match_str_alt_label_end',
            'match_str_last_part', 'match_str_first_part']

        if sum([getattr(self, f) for f in features]) == 0:
            self.match_str_conflict = 1
        else:
            self.match_str_conflict = 0

    def set_prob_features(self):
        '''
        Set the additional feature values needed for probability-based
        candidate ranking.
        '''
        # Mention representation
        self.set_entity_quotes()
        self.set_entity_type()

        # Description representation
        self.set_candidate_inlinks()
        self.set_candidate_ambig()
        self.set_candidate_lang()
        self.set_candidate_type()

        # Mention - description string match
        self.set_levenshtein()
        self.set_solr_properties()

        # Mention - description context match
        self.set_type_match()
        self.set_role_match()
        self.set_spec_match()
        self.set_keyword_match()
        self.set_subject_match()
        self.set_vector_match()
        self.set_entity_match()
        self.set_entity_match_newspapers()
        self.set_entity_vector_match()

    def set_entity_quotes(self):
        '''
        Count number of quotes surrounding entity mentions.
        '''
        if 'entity_quotes' not in self.features:
            return

        if not hasattr(self.cluster, 'sum_quotes'):
            self.cluster.sum_quotes = sum([e.quotes for e in
                self.cluster.entities])

        self.entity_quotes = math.tanh(self.cluster.sum_quotes * 0.25)

    def set_entity_type(self):
        '''
        Set entity type features.
        '''
        if not [f for f in self.features if f.startswith('entity_type')]:
            return

        if not hasattr(self.cluster, 'type_ratios'):
            self.cluster.get_type_ratios()

        type_ratios = self.cluster.type_ratios
        if not type_ratios:
            return

        for tr in type_ratios:
            setattr(self, 'entity_type_' + tr, type_ratios[tr])

    def set_candidate_inlinks(self):
        '''
        Determine inlinks feature values.
        '''
        if not [f for f in self.features if f.startswith('candidate_inlinks')]:
            return

        for link_type in ['inlinks', 'inlinks_newspapers']:
            link_count = self.document.get(link_type)
            if link_count:
                setattr(self, 'candidate_' + link_type,
                    math.tanh(link_count * 0.001))
                if not hasattr(self.cand_list, 'sum_' + link_type):
                    self.cand_list.set_sum_inlinks()
                link_sum = getattr(self.cand_list, 'sum_' + link_type)
                if link_sum:
                    link_count_rel = link_count / float(link_sum)
                    setattr(self, 'candidate_' + link_type + '_rel',
                        link_count_rel)

    def set_candidate_ambig(self):
        '''
        Determine if the description label is ambiguous.
        '''
        if 'candidate_ambig' not in self.features:
            return

        self.candidate_ambig = 1 if self.document.get('ambig') == 1 else -1

    def set_candidate_lang(self):
        '''
        Determine if description is available in Dutch.
        '''
        if 'candidate_lang' not in self.features:
            return

        self.candidate_lang = 1 if self.document.get('lang') == 'nl' else -1

    def set_candidate_type(self):
        '''
        Set candidate type features.
        '''
        if not [f for f in self.features if f.startswith('candidate_type')]:
            return

        schema_types = []
        if self.document.get('schema_type'):
            schema_types += self.document.get('schema_type')
        if self.document.get('dbo_type'):
            schema_types += self.document.get('dbo_type')
        if not schema_types:
            return

        for t in dictionary.types:
            for s in schema_types:
                if s in dictionary.types[t]['schema_types']:
                    setattr(self, 'candidate_type_' + t, 1)
                    break

    def set_levenshtein(self):
        '''
        Mean and max Levenshtein ratio for all labels.
        '''
        if not [f for f in self.features if f.startswith('match_str_lsr')]:
            return

        ne = self.cluster.entities[0].norm

        # Pref label
        l = self.document.get('pref_label')
        self.match_str_lsr_pref = Levenshtein.ratio(ne, l) * 2 - 1

        # Wikidata alt labels
        if self.document.get('wd_alt_label'):
            wd_labels = self.document.get('wd_alt_label')
            ratios = [Levenshtein.ratio(ne, l) for l in wd_labels]
            self.match_str_lsr_wd_max = max(ratios) * 2 - 1
            self.match_str_lsr_wd_mean = ((sum(ratios) / float(len(wd_labels)))
                * 2 - 1)
        else:
            wd_labels = []

        # Any other alt labels
        if self.document.get('alt_label'):
            labels = self.document.get('alt_label')
            labels = [l for l in labels if l not in wd_labels]
            if labels:
                ratios = [Levenshtein.ratio(ne, l) for l in labels]
                self.match_str_lsr_alt_max = max(ratios) * 2 - 1
                self.match_str_lsr_alt_mean = (sum(ratios) /
                        float(len(labels))) * 2 - 1

    def set_solr_properties(self):
        '''
        Determine Solr iteration, position and score.
        '''
        if not [f for f in self.features if f.startswith('match_str_solr')]:
            return

        # Solr iteration
        self.match_str_solr_query_0 = 1 if self.query_id == 0 else 0
        self.match_str_solr_query_1 = 1 if self.query_id == 1 else 0
        self.match_str_solr_query_2 = 1 if self.query_id == 2 else 0
        self.match_str_solr_query_3 = 1 if self.query_id == 3 else 0
        self.match_str_solr_substitution = 1 if self.query_iteration == 1 else 0

        # Solr position (relative to other remaining candidates)
        pos = self.cand_list.filtered_candidates.index(self)
        self.match_str_solr_position = 1.0 - math.tanh(pos * 0.25)

        # Solr score (relative to other remaining candidates)
        if not hasattr(self.cand_list, 'max_score'):
            self.cand_list.set_max_score()
        if self.cand_list.max_score:
            self.match_str_solr_score = (self.document.get('score') /
                float(self.cand_list.max_score))

    def set_type_match(self):
        '''
        Match entity and description type (person, location or organization).
        '''
        if not 'match_txt_type' in self.features:
            return

        if not hasattr(self.cluster, 'type_ratios'):
            self.cluster.get_type_ratios()

        type_ratios = self.cluster.type_ratios
        if not type_ratios:
            return

        schema_types = []
        if self.document.get('schema_type'):
            schema_types += self.document.get('schema_type')
        if self.document.get('dbo_type'):
            schema_types += self.document.get('dbo_type')

        # If no types available, try to deduce a type from the first sentence
        # of the abstract
        if not schema_types:
            if not hasattr(self, 'abstract_bow'):
                self.tokenize_abstract()
            bow = self.abstract_bow[:25]

            cand_types = []
            for role in [r for r in dictionary.roles if
                    len(dictionary.roles[r]['types']) == 1]:
                if len(set(bow) & set(dictionary.roles[role]['words'])) > 0:
                    cand_types.append(dictionary.roles[role]['types'][0])
            for t in dictionary.types:
                if len(set(bow) & set(dictionary.types[t]['words'])) > 0:
                    cand_types.append(t)
            if len(set(cand_types)) == 1:
                schema_types = dictionary.types[cand_types[0]]['schema_types']
            else:
                return

        # Matching type
        for r in type_ratios:
            if r in dictionary.types:
                for t in dictionary.types[r]['schema_types']:
                    if t in schema_types:
                        self.match_txt_type += type_ratios[r]
                        break
        if self.match_txt_type:
            return

        if len(type_ratios) == 1:
            # Non-matching: persons can't be locations or organizations
            if 'person' in type_ratios:
                for other in [t for t in dictionary.types if t != 'person']:
                    for t in dictionary.types[other]['schema_types']:
                        if t in schema_types:
                            self.match_txt_type = -1
                            return

            # Non-matching: locations and organizations can't be persons
            elif 'location' in type_ratios or 'organisation' in type_ratios:
                if 'Person' in schema_types:
                    self.match_txt_type = -1

    def set_role_match(self):
        '''
        Match entity and description role (e.g. minister, university, river).
        '''
        if not 'match_txt_role' in self.features:
            return

        roles = {e.role for e in self.cluster.entities if e.role}
        if not roles:
            return

        # Match schema.org and DBpedia ontology types
        schema_types = []
        if self.document.get('schema_type'):
            schema_types += self.document.get('schema_type')
        if self.document.get('dbo_type'):
            schema_types += self.document.get('dbo_type')
        if schema_types:
            for role in roles:
                for t in dictionary.roles[role]['schema_types']:
                    if t in schema_types:
                        self.match_txt_role = 1
                        return

        # Match first sentence abstract
        if not hasattr(self, 'abstract_bow'):
            self.tokenize_abstract()
        bow = self.abstract_bow[:25]

        for role in roles:
            if len(set(bow) & set(dictionary.roles[role]['words'])) > 0:
                self.match_txt_role = 1
                return

        # Check for conflict
        if schema_types:
            for role in [r for r in dictionary.roles if r not in roles]:
                for t in dictionary.roles[role]['schema_types']:
                    if t in schema_types:
                        self.match_txt_role = -1
                        return

    def set_spec_match(self):
        '''
        Find the specification between brackets in the description uri in
        the article.
        '''
        if not 'match_txt_spec' in self.features:
            return

        spec = self.document.get('spec')
        if spec:
            spec_stem = spec[:int(math.ceil(len(spec) * 0.8))]
        else:
            return

        if not hasattr(self.cluster.context, 'ocr_norm'):
            self.cluster.context.normalize_ocr()

        ocr = self.cluster.context.ocr_norm
        if ocr.find(spec_stem) > -1:
            self.match_txt_spec = 1

    def set_keyword_match(self):
        '''
        Match DBpedia category keywords with the article ocr.
        '''
        if not 'match_txt_keyword' in self.features:
            return

        if not self.document.get('keyword'):
            return

        key_stems = [w[:int(math.ceil(len(w) * 0.8))] for w in
            self.document.get('keyword') if w not in dictionary.unwanted]
        if not key_stems:
            return

        if not hasattr(self.cluster.context, 'ocr_bow'):
            self.cluster.context.tokenize_ocr()

        bow = self.cluster.context.ocr_bow
        key_match = len([w for w in bow for s in key_stems if w.startswith(s)])
        self.match_txt_keyword = math.tanh(key_match * 0.25)

    def set_subject_match(self):
        '''
        Match the subject areas identified for the article with the DBpedia
        abstract.
        '''
        if not 'match_txt_subject' in self.features:
            return

        if not hasattr(self.cluster.context, 'subjects'):
            self.cluster.context.get_subjects()

        subjects = self.cluster.context.subjects
        if not subjects:
            return

        if not hasattr(self, 'abstract_bow'):
            self.tokenize_abstract()
        bow = self.abstract_bow

        subject_match = 0
        for subject in subjects:
            words = dictionary.subjects[subject]
            for role in dictionary.roles:
                if subject in dictionary.roles[role]['subjects']:
                    words += dictionary.roles[role]['words']
            if len(set(words) & set(bow)) > 0:
                subject_match += 1

        # Check for conflicts
        if subject_match == 0:
            for subject in [s for s in dictionary.subjects if s not in
                    subjects]:
                words = dictionary.subjects[subject]
                for role in dictionary.roles:
                    if subject in dictionary.roles[role]['subjects']:
                        if len(set(dictionary.roles[role]['subjects']) &
                                set(subjects)) == 0:
                            words += dictionary.roles[role]['words']
                if len(set(words) & set(bow)) > 0:
                    subject_match = -1

        if subject_match > 0:
            self.match_txt_subject = math.tanh(subject_match * 0.25)
        elif subject_match < -1:
            self.match_txt_subject = math.tanh((subject_match + 1) * 0.25)

    def set_vector_match(self):
        '''
        Match context word vectors with abstract word vectors.
        '''
        if not [f for f in self.features if f.startswith('match_txt_vec')]:
            return

        if not self.document.get('lang') == 'nl':
            return

        if not hasattr(self.cluster, 'window'):
            self.cluster.get_window()
        if not self.cluster.window:
            return

        if not hasattr(self, 'abstract_bow'):
            self.tokenize_abstract()
        bow = [w for w in self.abstract_bow[:25] if len(w) > 4 and w not in
            self.cluster.entity_parts and w not in dictionary.unwanted]
        if self.document.get('keyword'):
            bow += [w for w in self.document.get('keyword') if len(w) > 4 and w
                not in self.cluster.entity_parts and w not in
                dictionary.unwanted]
        if not bow:
            return

        if not hasattr(self.cluster, 'window_vectors'):
            self.cluster.window_vectors = self.get_vectors(self.cluster.window,
                    service=W2V_URL)
        if not self.cluster.window_vectors:
            return

        cand_vectors = self.get_vectors(bow, service=W2V_URL)
        if not cand_vectors:
            return

        sims = cosine_similarity(np.array(self.cluster.window_vectors),
            np.array(cand_vectors))

        self.match_txt_vec_max = sims.max() - 0.25
        self.match_txt_vec_mean = sims.mean()

    def set_entity_match(self):
        '''
        Match other entities appearing in the article with DBpedia abstract.
        '''
        if not 'match_txt_entities' in self.features:
            return

        if not hasattr(self.cluster, 'entity_parts'):
            self.cluster.get_entity_parts()
        if not hasattr(self.cluster, 'context_entity_parts'):
            self.cluster.get_context_entity_parts()
        if not self.cluster.context_entity_parts:
            return

        if not hasattr(self, 'abstract_bow'):
            self.tokenize_abstract()
        bow = [t for t in self.abstract_bow if len(t) > 4]

        entity_match = len(set(self.cluster.context_entity_parts) & set(bow))
        self.match_txt_entities = math.tanh(entity_match * 0.25)

    def set_entity_match_newspapers(self):
        '''
        Get number of newspaper articles where candidate pref label appears
        together with other entity mentions in the article.
        '''
        if not 'match_txt_entities_newspapers' in self.features:
            return

        # Candidate has to be person
        if not self.document.get('last_part'):
            return

        # Candidate pref label can't be ambiguous
        if self.document.get('ambig') == 1:
            return

        # Candidate pref label has to appear in newspapers on its own
        if not self.document.get('inlinks_newspapers'):
            return

        # Normalized entity has to differ from pref label
        pref_label = self.document.get('pref_label')
        if self.match_str_pref_label_exact:
            return
        # But partly match or last part match
        if not (self.match_str_pref_label_end or self.match_str_pref_label or
                self.match_str_last_part):
            return

        # Other entity mentions have to be available from context
        context_entities = [e.norm for e in self.cluster.context.entities if
            e.norm.find(self.cluster.entities[0].norm) == -1 and
            e.norm.find(pref_label) == -1]
        if not context_entities:
            return

        # Query for co-occurence
        query = '"' + pref_label + '" AND ('
        for i, e in enumerate(context_entities):
            if i > 0:
                query += ' OR '
            query += '"' + e + '"'
        query += ')'

        payload = {}
        payload['operation'] = 'searchRetrieve'
        payload['x-collection'] = 'DDD_artikel'
        payload['maximumRecords'] = 0
        payload['query'] = query

        try:
            response = requests.get(JSRU_URL, params=payload, timeout=60)
            xml = etree.fromstring(response.content)
            tag = '{http://www.loc.gov/zing/srw/}numberOfRecords'
            num_records = int(xml.find(tag).text)
            self.match_txt_entities_newspapers = (num_records /
                    float(self.document.get('inlinks_newspapers')))
        except:
            return

    def set_entity_vector_match(self):
        '''
        Match word vectors for other entities in the article with entity vector.
        '''
        if not [f for f in self.features if
                f.startswith('match_txt_entity_vec')]:
            return

        wd_id = self.document.get('uri_wd')
        if not wd_id:
            return

        wd_id = wd_id.split('/')[-1]

        if not hasattr(self.cluster, 'context_entity_parts'):
            self.cluster.get_context_entity_parts()
        if not self.cluster.context_entity_parts:
            return

        if not hasattr(self.cluster, 'context_entity_vectors'):
            self.cluster.context_entity_vectors = self.get_vectors(
                    self.cluster.context_entity_parts)
        if not self.cluster.context_entity_vectors:
            return

        cand_vectors = self.get_vectors([wd_id])
        if not cand_vectors:
            return

        sims = cosine_similarity(np.array(self.cluster.context_entity_vectors),
            np.array(cand_vectors))
        self.match_txt_entity_vec_max = sims.max() - 0.25
        self.match_txt_entity_vec_mean = sims.mean() - 0.2

    def tokenize_abstract(self):
        '''
        Tokenize and normalize DBpedia abstract.
        '''
        abstract = self.document.get('abstract')
        self.abstract_bow = [w for t in utilities.tokenize(abstract) for w in
            utilities.normalize(t).split()]
        self.abstract_bow = list(set(self.abstract_bow))

    def get_vectors(self, wordlist, service=W2V_URL):
        '''
        Get word vectors for given word list.
        '''
        payload = {'source': ' '.join(wordlist)}
        response = requests.get(service, params=payload, timeout=180)
        assert response.status_code == 200, 'Error retrieving word vectors'
        data = response.json()
        return data['vectors']


class Result():
    '''
    The link result for an entity cluster.
    '''

    def __init__(self, reason, prob=0, description=None, cand_list=None):
        '''
        Set the result attributes.
        '''
        self.reason = reason
        self.prob = prob
        self.description = description

        self.link = None
        self.label = None
        self.features = None
        self.candidates = None

        if description:
            self.features = {}
            for f in description.features:
                self.features[f] = float(getattr(description, f))
            if self.prob >= MIN_PROB:
                self.link = description.document.get('id')
                self.label = description.document.get('label')

        if cand_list:
            self.candidates = []
            for description in cand_list.candidates:
                d = {}
                d['id'] = description.document.get('id')
                d['prob'] = description.prob
                d['features'] = {}
                for f in description.features:
                    d['features'][f] = float(getattr(description, f))
                d['document'] = description.document
                self.candidates.append(d)

    def get_dict(self, features=False, candidates=False):
        '''
        Return the result dictionary.
        '''
        result = {}
        result['reason'] = self.reason
        if self.prob:
            result['prob'] = self.prob
        if self.link:
            result['link'] = self.link
        if self.label:
            result['label'] = self.label
        if features and self.features:
            result['features'] = self.features
        if candidates and self.candidates:
            result['candidates'] = self.candidates
        return result


if __name__ == '__main__':
    import pprint

    if not len(sys.argv) > 1:
        print("Usage: ./dac.py [url (string)]")

    else:
        linker = EntityLinker(model='bnn', debug=True, train=True,
            features=True, candidates=True)
        if len(sys.argv) > 2:
            pprint.pprint(linker.link(sys.argv[1], sys.argv[2]))
        else:
            pprint.pprint(linker.link(sys.argv[1]))
