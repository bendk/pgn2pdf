#!/usr/bin/env python

import collections
import glob
import itertools
import os
from os import path
import re
import shutil
import subprocess
import sys
import tempfile
import textwrap

# really dumb handling of the PGN grammer
PGNElement = collections.namedtuple('PGNElement', 'name text')

class PGNParser(object):
    header_re = re.compile(r' *\[([^ ]+) "([^"]+)"\]')

    # regexes to parse the game tree.  For each of these, the first group is
    # the text of the PGNElement to create.
    game_regexes = [
        ('comment', re.compile(r'{([^}]*)}')),
        ('start-variation', re.compile(r'\(')),
        ('end-variation', re.compile(r'\)')),
        ('evaluation', re.compile(r'([+-/=]{1,3})')),
        ('result', re.compile(r'((1-0)|(0-1)|(1/2-1/2))')),
    ]

    def __init__(self, path):
        content = open(path).read()
        game_content = self.parse_headers(content)
        self.parse_game(game_content)
        self.combine_moves()
        self.game.append(PGNElement('end', ''))

    def parse_headers(self, content):
        self.headers = {}
        lines = content.split("\n")
        for i, line in enumerate(lines):
            if not line.strip():
                # done with headers, return the rest of the content
                return '\n'.join(lines[i+1:])
            match = self.header_re.match(line)
            if match is None:
                raise ValueError("Cant parse header: %s" % line)
            self.headers[match.group(1).lower()] = match.group(2)

    def parse_game(self, game_content):
        self.game = []
        while True:
            game_content = game_content.lstrip()
            if not game_content:
                break
            for name, regex in self.game_regexes:
                match = regex.match(game_content)
                if match is None:
                    continue
                try:
                    text = match.group(1)
                except IndexError:
                    text = ''
                self.game.append(PGNElement(name, text))
                game_content = game_content[match.end():]
                break
            else:
                # assume anything else is a move
                move_text, game_content = game_content.split(None, 1)
                self.game.append(PGNElement('move', move_text))

    def combine_moves(self):
        current_moves = []
        new_game = []
        for elt in self.game:
            if elt.name == 'move':
                current_moves.append(elt.text)
            else:
                if current_moves:
                    moves = ' '.join(current_moves)
                    new_game.append(PGNElement('moves', moves))
                    current_moves = []
                new_game.append(elt)
        self.game = new_game

class TEXWriter(object):
    """Write tex files."""
    def __init__(self, path):
        self.out = open(path, 'w')
        self.variation_counter = itertools.count()
        self.variation_stack = []

    def write(self, line, *args, **kwargs):
        paragraph = kwargs.get("paragraph", False)
        trailing_newline = kwargs.get("trailing_newline", False)
        line = line % args
        if paragraph:
            self.out.write("\n")
        self.out.write('\n'.join(textwrap.wrap(line, 78)) + '\n')
        if paragraph or trailing_newline:
            self.out.write("\n")

    def start(self):
        self.write('\\documentclass[a4paper]{article}')
        self.write('\\usepackage{xskak}')
        self.write('\\setlength{\\parskip}{1em}')
        self.write('\\begin{document}')
        self.write('')
        self.write('\\newchessgame[id=main]')

    def write_title(self, title):
        self.write("\section{%s}", title)

    def make_diagram(self):
        self.write("\\chessboard", paragraph=True)

    def cur_var(self):
        try:
            return self.variation_stack[-1]
        except IndexError:
            return 'main'

    def start_variation(self):
        var = 'var%s' % (self.variation_counter.next())
        self.write('\\newchessgame[newvar=%s, id=%s]', self.cur_var(), var)
        self.variation_stack.append(var)

    def end_variation(self):
        self.variation_stack.pop()
        self.write('\\resumechessgame[id=%s]', self.cur_var(),
                   trailing_newline=len(self.variation_stack) == 0)

    def setup_board(self, fen):
        self.write('\\fenboard{%s}', fen)

    def write_moves(self, moves):
        self.write('\\mainline { %s }', moves)

    def end(self):
        self.write('')
        self.write("\end{document}")
        self.out.close()

class PGN2PDF(object):
    def __init__(self, args):
        self.pgn = PGNParser(args[0])
        self.setup_tex_writer(args[0])
        self.game_index = 0
        try:
            self.convert()
            if len(args) > 1:
                self.write_pdf(args[1])
            else:
                self.print_tex()
        finally:
            self.cleanup_tex()

    def setup_tex_writer(self, pgn_path):
        tex_filename = path.splitext(path.basename(pgn_path))[0] + '.tex'
        self.tempdir = tempfile.mkdtemp()
        self.tex_path = path.join(self.tempdir, tex_filename)
        self.tex = TEXWriter(self.tex_path)

    def convert(self):
        self.tex.start()
        self.tex.write_title(self.make_title())
        if 'fen' in self.pgn.headers:
            self.tex.setup_board(self.pgn.headers['fen'])
            self.tex.make_diagram()
        self.write_game()
        self.tex.end()

    def make_title(self):
        title = '%s - %s' % (self.make_name('white'), self.make_name('black'))
        if 'site' in self.pgn.headers and 'date' in self.pgn.headers:
            title += ' %s %s' % (self.pgn.headers['site'],
                    self.pgn.headers['date'].split('.')[0])
        return title

    def make_name(self, color):
        return self.pgn.headers[color].split(',')[0]

    def game_iterator(self):
        while self.game_index < len(self.pgn.game):
            elt = self.current_elt
            self.advance_game_index()
            yield elt

    @property
    def current_elt(self):
        return self.pgn.game[self.game_index]

    def advance_game_index(self):
        self.game_index += 1

    def write_game(self):
        for elt in self.game_iterator():
            if elt.name == 'moves':
                self.tex.write_moves(elt.text)
            elif elt.name == 'comment':
                self.parse_comment(elt.text)
            elif elt.name == 'evaluation':
                self.tex.write(elt.text)
            elif elt.name == 'result':
                self.tex.write(elt.text, paragraph=True)
            elif elt.name == 'start-variation':
                self.tex.write('(')
                self.tex.start_variation()
            elif elt.name == 'end-variation':
                self.tex.write(')')
                self.tex.end_variation()
            elif elt.name == 'end':
                break
            else:
                raise ValueError("Unknown PGN element: %s" % elt.name)

    def parse_comment(self, text):
        if text[0].islower():
            # lowercase text in the PGN comment means we want to write our
            # comment inline -- no new paragraph.
            self.tex.write(text)
            return

        # We're going to be writing this comment as a new paragraph.  One
        # corner case is if this comment is the very end of a variation.  In
        # that case, we should write the ")" for the end of the variation first
        # rather than have it dangling after the paragraph

        if self.current_elt.name == 'end-variation':
            self.tex.write(')')
            self.advance_game_index()
            end_variation = True
        else:
            end_variation = False

        if text.startswith("(D)"):
            self.tex.make_diagram()
            text = text[3:].strip()

        for line in text.split("\n"):
            line = line.strip()
            if not line:
                continue
            self.tex.write(line, paragraph=True)

        if end_variation:
            self.tex.end_variation()
            self.tex.write('')

    def print_tex(self):
        print open(self.tex_path).read()

    def write_pdf(self, output_path):
        subprocess.call([
            'pdflatex',
            '-output-directory',
            self.tempdir,
            self.tex_path
        ])
        pdf_path = glob.glob(path.join(self.tempdir, '*.pdf'))[0]
        shutil.move(pdf_path, output_path)

    def cleanup_tex(self):
        shutil.rmtree(self.tempdir)

if __name__ == '__main__':
    PGN2PDF(sys.argv[1:])

