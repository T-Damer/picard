# -*- coding: utf-8 -*-
#
# Picard, the next-generation MusicBrainz tagger
#
# Copyright (C) 2017 David Mandelberg
# Copyright (C) 2017-2018 Sambhav Kothari
# Copyright (C) 2017-2020 Laurent Monin
# Copyright (C) 2018-2021 Philipp Wolfer
# Copyright (C) 2019 Michael Wiencek
# Copyright (C) 2020 David Kellner
# Copyright (C) 2020 dukeyin
# Copyright (C) 2021 Vladislav Karbovskii
# Copyright (C) 2021 Bob Swift
#
# This program is free software; you can redistribute it and/or
# modify it under the terms of the GNU General Public License
# as published by the Free Software Foundation; either version 2
# of the License, or (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program; if not, write to the Free Software
# Foundation, Inc., 51 Franklin Street, Fifth Floor, Boston, MA 02110-1301, USA.


from picard import log
from picard.config import get_config
from picard.const import RELEASE_FORMATS
from picard.util import (
    format_time,
    linear_combination_of_weights,
    parse_amazon_url,
    translate_from_sortname,
)
from picard.util.script_detector_weighted import detect_script_weighted


_artist_rel_types = {
    "arranger": "arranger",
    "audio": "engineer",
    "chorus master": "performer:chorus master",
    "composer": "composer",
    "concertmaster": "performer:concertmaster",
    "conductor": "conductor",
    "engineer": "engineer",
    "instrument arranger": "arranger",
    "librettist": "lyricist",
    "live sound": "engineer",
    "lyricist": "lyricist",
    # "mastering": "engineer",
    "mix-DJ": "djmixer",
    "mix": "mixer",
    "orchestrator": "arranger",
    "performing orchestra": "performer:orchestra",
    "producer": "producer",
    # "recording": "engineer",
    "remixer": "remixer",
    "sound": "engineer",
    "video director": "director",
    "vocal arranger": "arranger",
    "writer": "writer",
}

_TRACK_TO_METADATA = {
    'number': '~musicbrainz_tracknumber',
    'position': 'tracknumber',
    'title': 'title',
}

_MEDIUM_TO_METADATA = {
    'format': 'media',
    'position': 'discnumber',
    'title': 'discsubtitle',
    'track-count': 'totaltracks',
}

_RECORDING_TO_METADATA = {
    'disambiguation': '~recordingcomment',
    'first-release-date': '~recording_firstreleasedate',
    'title': 'title',
}

_RELEASE_TO_METADATA = {
    'annotation': '~releaseannotation',
    'asin': 'asin',
    'barcode': 'barcode',
    'country': 'releasecountry',
    'date': 'date',
    'disambiguation': '~releasecomment',
    'title': 'album',
}

_ARTIST_TO_METADATA = {
    'gender': 'gender',
    'name': 'name',
    'type': 'type',
}

_RELEASE_GROUP_TO_METADATA = {
    'disambiguation': '~releasegroupcomment',
    'first-release-date': '~releasegroup_firstreleasedate',
    'title': '~releasegroup',
}


_REPLACE_MAP = {}
_PREFIX_ATTRS = ['guest', 'additional', 'minor', 'solo']
_BLANK_SPECIAL_RELTYPES = {'vocal': 'vocals'}


def _transform_attribute(attr, attr_credits):
    if attr in attr_credits:
        return attr_credits[attr]
    else:
        return _REPLACE_MAP.get(attr, attr)


def _parse_attributes(attrs, reltype, attr_credits):
    prefixes = []
    nouns = []
    for attr in attrs:
        attr = _transform_attribute(attr, attr_credits)
        if attr in _PREFIX_ATTRS:
            prefixes.append(attr)
        else:
            nouns.append(attr)
    prefix = ' '.join(prefixes)
    if len(nouns) > 1:
        result = '%s and %s' % (', '.join(nouns[:-1]), nouns[-1:][0])
    elif len(nouns) == 1:
        result = nouns[0]
    else:
        result = _BLANK_SPECIAL_RELTYPES.get(reltype, '')
    return ' '.join([prefix, result]).strip()


def _relations_to_metadata(relations, m):
    config = get_config()
    use_credited_as = not config.setting['standardize_artists']
    use_instrument_credits = not config.setting['standardize_instruments']
    for relation in relations:
        if relation['target-type'] == 'artist':
            artist = relation['artist']
            value, valuesort = _translate_artist_node(artist)
            has_translation = (value != artist['name'])
            if not has_translation and use_credited_as and 'target-credit' in relation:
                credited_as = relation['target-credit']
                if credited_as:
                    value = credited_as
            reltype = relation['type']
            attribs = []
            if 'attributes' in relation:
                attribs = [a for a in relation['attributes']]
            if reltype in {'vocal', 'instrument', 'performer'}:
                if use_instrument_credits:
                    attr_credits = relation.get('attribute-credits', {})
                else:
                    attr_credits = {}
                name = 'performer:' + _parse_attributes(attribs, reltype, attr_credits)
            elif reltype == 'mix-DJ' and len(attribs) > 0:
                if not hasattr(m, "_djmix_ars"):
                    m._djmix_ars = {}
                for attr in attribs:
                    m._djmix_ars.setdefault(attr.split()[1], []).append(value)
                continue
            else:
                try:
                    name = _artist_rel_types[reltype]
                except KeyError:
                    continue
            if value not in m[name]:
                m.add(name, value)
            if name == 'composer' and valuesort not in m['composersort']:
                m.add('composersort', valuesort)
        elif relation['target-type'] == 'work':
            if relation['type'] == 'performance':
                performance_to_metadata(relation, m)
                work_to_metadata(relation['work'], m)
        elif relation['target-type'] == 'url':
            if relation['type'] == 'amazon asin' and 'asin' not in m:
                amz = parse_amazon_url(relation['url']['resource'])
                if amz is not None:
                    m['asin'] = amz['asin']
            elif relation['type'] == 'license':
                url = relation['url']['resource']
                m.add('license', url)


def _translate_artist_node(node):
    config = get_config()
    transl, translsort = None, None
    if config.setting['translate_artist_names']:
        if config.setting['translate_artist_names_script_exception']:
            log_text = 'Script alpha characters found in "{0}": '.format(node["name"],)
            detected_scripts = detect_script_weighted(node["name"])
            if detected_scripts:
                log_text += "; ".join(
                    list("{0} ({1:.1f}%)".format(scr_id, detected_scripts[scr_id] * 100) for scr_id in detected_scripts)
                )
            else:
                log_text += "None"
            log.debug(log_text)
            if detected_scripts:
                if config.setting["script_exceptions"]:
                    log_text = " found in selected scripts: " + "; ".join(
                        list("{0} ({1}%)".format(scr[0], scr[1]) for scr in config.setting["script_exceptions"])
                    )
                    for script_id, script_weighting in config.setting["script_exceptions"]:
                        if script_id in detected_scripts and detected_scripts[script_id] >= script_weighting / 100:
                            log.debug("Match" + log_text)
                            return node['name'], node['sort-name']
                    log.debug("No match" + log_text)
                else:
                    log.warning("No scripts selected for translation exception match check.")

        def check_higher_score(locale_dict, locale, score):
            return locale not in locale_dict or score > locale_dict[locale][0]

        # Prepare dictionaries of available locale aliases
        full_locales = {}
        root_locales = {}
        if "aliases" in node:
            for alias in node['aliases']:
                if not alias["primary"]:
                    continue
                if "locale" not in alias:
                    continue
                full_locale = alias['locale']
                root_locale = full_locale.split("_")[0]
                full_parts = []
                root_parts = []
                score = 0.8
                full_parts.append((score, 5))
                if '_' in full_locale:
                    score = 0.4
                root_parts.append((score, 5))
                if alias["type"] == "Artist name":
                    score = 0.8
                elif alias["type"] == "Legal Name":
                    score = 0.5
                else:
                    # as 2014/09/19, only Artist or Legal names should have the
                    # Primary flag
                    score = 0.0
                full_parts.append((score, 5))
                root_parts.append((score, 5))
                comb = linear_combination_of_weights(full_parts)
                if check_higher_score(full_locales, full_locale, comb):
                    full_locales[full_locale] = (comb, (alias['name'], alias["sort-name"]))
                comb = linear_combination_of_weights(root_parts)
                if check_higher_score(root_locales, root_locale, comb):
                    root_locales[root_locale] = (comb, (alias['name'], alias["sort-name"]))

            # First pass to match full locale if available
            for locale in config.setting["artist_locales"]:
                if locale in full_locales:
                    return full_locales[locale][1]

            # Second pass to match root locale if available
            for locale in config.setting["artist_locales"]:
                lang = locale.split("_")[0]
                if lang in root_locales:
                    return root_locales[lang][1]

        # No matches found in available alias locales
        translsort = node['sort-name']
        transl = translate_from_sortname(node['name'] or "", translsort)
    else:
        transl, translsort = node['name'], node['sort-name']
    return (transl, translsort)


def artist_credit_from_node(node):
    artist = ""
    artistsort = ""
    artists = []
    artistssort = []
    config = get_config()
    use_credited_as = not config.setting["standardize_artists"]
    for artist_info in node:
        a = artist_info['artist']
        translated, translated_sort = _translate_artist_node(a)
        has_translation = (translated != a['name'])
        if has_translation:
            name = translated
        elif use_credited_as and 'name' in artist_info:
            name = artist_info['name']
        else:
            name = a['name']
        artist += name
        artistsort += translated_sort or ""
        artists.append(name)
        artistssort.append(translated_sort)
        if 'joinphrase' in artist_info:
            artist += artist_info['joinphrase'] or ""
            artistsort += artist_info['joinphrase'] or ""
    return (artist, artistsort, artists, artistssort)


def artist_credit_to_metadata(node, m, release=False):
    ids = [n['artist']['id'] for n in node]
    artist, artistsort, artists, artistssort = artist_credit_from_node(node)
    if release:
        m["musicbrainz_albumartistid"] = ids
        m["albumartist"] = artist
        m["albumartistsort"] = artistsort
        m["~albumartists"] = artists
        m["~albumartists_sort"] = artistssort
    else:
        m["musicbrainz_artistid"] = ids
        m["artist"] = artist
        m["artistsort"] = artistsort
        m["artists"] = artists
        m["~artists_sort"] = artistssort


def countries_from_node(node):
    countries = []
    if "release-events" in node:
        for release_event in node['release-events']:
            try:
                country_code = release_event['area']['iso-3166-1-codes'][0]
            # TypeError in case object is None
            except (KeyError, IndexError, TypeError):
                pass
            else:
                if country_code:
                    countries.append(country_code)
    return countries


def release_dates_and_countries_from_node(node):
    dates = []
    countries = []
    if "release-events" in node:
        for release_event in node['release-events']:
            dates.append(release_event['date'] or '')
            country_code = ''
            try:
                country_code = release_event['area']['iso-3166-1-codes'][0]
            # TypeError in case object is None
            except (KeyError, IndexError, TypeError):
                pass
            countries.append(country_code)
    return dates, countries


def label_info_from_node(node):
    labels = []
    catalog_numbers = []
    for label_info in node:
        if 'label' in label_info and label_info['label'] and 'name' in label_info['label']:
            label = label_info['label']['name']
            if label and label not in labels:
                labels.append(label)
        if 'catalog-number' in label_info:
            cat_num = label_info['catalog-number']
            if cat_num and cat_num not in catalog_numbers:
                catalog_numbers.append(cat_num)
    return (labels, catalog_numbers)


def media_formats_from_node(node):
    formats_count = {}
    formats_order = []
    for medium in node:
        text = medium.get('format', "(unknown)") or "(unknown)"
        if text in formats_count:
            formats_count[text] += 1
        else:
            formats_count[text] = 1
            formats_order.append(text)
    formats = []
    for medium_format in formats_order:
        count = formats_count[medium_format]
        medium_format = RELEASE_FORMATS.get(medium_format, medium_format)
        if count > 1:
            medium_format = str(count) + "×" + medium_format
        formats.append(medium_format)
    return " + ".join(formats)


def track_to_metadata(node, track):
    m = track.metadata
    recording_to_metadata(node['recording'], m, track)
    m.add_unique('musicbrainz_trackid', node['id'])
    # overwrite with data we have on the track
    for key, value in node.items():
        if not value and value != 0:
            continue
        if key in _TRACK_TO_METADATA:
            m[_TRACK_TO_METADATA[key]] = value
        elif key == 'length' and value:
            m.length = value
        elif key == 'artist-credit':
            artist_credit_to_metadata(value, m)
    if m.length:
        m['~length'] = format_time(m.length)


def recording_to_metadata(node, m, track=None):
    m.length = 0
    m.add_unique('musicbrainz_recordingid', node['id'])
    for key, value in node.items():
        if not value and value != 0:
            continue
        if key in _RECORDING_TO_METADATA:
            m[_RECORDING_TO_METADATA[key]] = value
        elif key == 'user-rating':
            m['~rating'] = value['value']
        elif key == 'length':
            m.length = value
        elif key == 'artist-credit':
            artist_credit_to_metadata(value, m)
            # set tags from artists
            if track:
                for credit in value:
                    artist = credit['artist']
                    artist_obj = track.append_track_artist(artist['id'])
                    add_genres_from_node(artist, artist_obj)
        elif key == 'relations':
            _relations_to_metadata(value, m)
        elif track and key in {'genres', 'tags'}:
            add_genres(value, track)
        elif track and key in {'user-genres', 'user-tags'}:
            add_user_genres(value, track)
        elif key == 'isrcs':
            add_isrcs_to_metadata(value, m)
        elif key == 'video' and value:
            m['~video'] = '1'
    if m['title']:
        m['~recordingtitle'] = m['title']
    if m.length:
        m['~length'] = format_time(m.length)
    if 'instrumental' in m.getall('~performance_attributes'):
        m.unset('lyricist')
        m['language'] = 'zxx'


def performance_to_metadata(relation, m):
    if 'attributes' in relation:
        for attribute in relation['attributes']:
            m.add_unique("~performance_attributes", attribute)


def work_to_metadata(work, m):
    m.add_unique("musicbrainz_workid", work['id'])
    if 'languages' in work:
        for language in work['languages']:
            m.add_unique("language", language)
    elif 'language' in work:
        m.add_unique("language", work['language'])
    if 'title' in work:
        m.add_unique("work", work['title'])
    if 'disambiguation' in work:
        m.add_unique("~workcomment", work['disambiguation'])
    if 'relations' in work:
        _relations_to_metadata(work['relations'], m)


def medium_to_metadata(node, m):
    for key, value in node.items():
        if not value and value != 0:
            continue
        if key in _MEDIUM_TO_METADATA:
            m[_MEDIUM_TO_METADATA[key]] = value


def artist_to_metadata(node, m):
    """Make meatadata dict from a JSON 'artist' node."""
    m.add_unique("musicbrainz_artistid", node['id'])
    for key, value in node.items():
        if not value and value != 0:
            continue
        if key in _ARTIST_TO_METADATA:
            m[_ARTIST_TO_METADATA[key]] = value
        elif key == "area":
            m["area"] = value['name']
        elif key == "life-span":
            if "begin" in value:
                m["begindate"] = value['begin']
            if "ended" in value:
                ended = value['ended']
                if ended and "end" in value:
                    m["enddate"] = value['end']
        elif key == "begin-area":
            m["beginarea"] = value['name']
        elif key == "end-area":
            m["endarea"] = value['name']


def release_to_metadata(node, m, album=None):
    """Make metadata dict from a JSON 'release' node."""
    config = get_config()
    m.add_unique('musicbrainz_albumid', node['id'])
    for key, value in node.items():
        if not value and value != 0:
            continue
        if key in _RELEASE_TO_METADATA:
            m[_RELEASE_TO_METADATA[key]] = value
        elif key == 'status':
            m['releasestatus'] = value.lower()
        elif key == 'artist-credit':
            artist_credit_to_metadata(value, m, release=True)
            # set tags from artists
            if album is not None:
                for credit in value:
                    artist = credit['artist']
                    artist_obj = album.append_album_artist(artist['id'])
                    add_genres_from_node(artist, artist_obj)
        elif key == 'relations' and config.setting['release_ars']:
            _relations_to_metadata(value, m)
        elif key == 'label-info':
            m['label'], m['catalognumber'] = label_info_from_node(value)
        elif key == 'text-representation':
            if 'language' in value:
                m['~releaselanguage'] = value['language']
            if 'script' in value:
                m['script'] = value['script']
    m['~releasecountries'] = release_countries = countries_from_node(node)
    # The MB web service returns the first release country in the country tag.
    # If the user has configured preferred release countries, use the first one
    # if it is one in the complete list of release countries.
    for country in config.setting["preferred_release_countries"]:
        if country in release_countries:
            m['releasecountry'] = country
            break
    add_genres_from_node(node, album)


def release_group_to_metadata(node, m, release_group=None):
    """Make metadata dict from a JSON 'release-group' node taken from inside a 'release' node."""
    m.add_unique('musicbrainz_releasegroupid', node['id'])
    for key, value in node.items():
        if not value and value != 0:
            continue
        if key in _RELEASE_GROUP_TO_METADATA:
            m[_RELEASE_GROUP_TO_METADATA[key]] = value
        elif key == 'primary-type':
            m['~primaryreleasetype'] = value.lower()
        elif key == 'secondary-types':
            add_secondary_release_types(value, m)
    add_genres_from_node(node, release_group)
    if m['~releasegroup_firstreleasedate']:
        m['originaldate'] = m['~releasegroup_firstreleasedate']
        m['originalyear'] = m['originaldate'][:4]
    m['releasetype'] = m.getall('~primaryreleasetype') + m.getall('~secondaryreleasetype')


def add_secondary_release_types(node, m):
    for secondary_type in node:
        m.add_unique('~secondaryreleasetype', secondary_type.lower())


def add_genres_from_node(node, obj):
    if obj is None:
        return
    if 'genres' in node:
        add_genres(node['genres'], obj)
    if 'tags' in node:
        add_genres(node['tags'], obj)
    if 'user-genres' in node:
        add_user_genres(node['user-genres'], obj)
    if 'user-tags' in node:
        add_user_genres(node['user-tags'], obj)


def add_genres(node, obj):
    for tag in node:
        key = tag['name']
        count = tag['count']
        if key:
            obj.add_genre(key, count)


def add_user_genres(node, obj):
    for tag in node:
        key = tag['name']
        if key:
            obj.add_genre(key, 1)


def add_isrcs_to_metadata(node, metadata):
    for isrc in node:
        metadata.add('isrc', isrc)


def get_score(node):
    """Returns the score attribute for a node.
    The score is expected to be an integer between 0 and 100, it is returned as
    a value between 0.0 and 1.0. If there is no score attribute or it has an
    invalid value 1.0 will be returned.
    """
    try:
        return int(node.get('score', 100)) / 100
    except (TypeError, ValueError):
        return 1.0
