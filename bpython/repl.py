# The MIT License
#
# Copyright (c) 2009-2010 the bpython authors.
#
# Permission is hereby granted, free of charge, to any person obtaining a copy
# of this software and associated documentation files (the "Software"), to deal
# in the Software without restriction, including without limitation the rights
# to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
# copies of the Software, and to permit persons to whom the Software is
# furnished to do so, subject to the following conditions:
#
# The above copyright notice and this permission notice shall be included in
# all copies or substantial portions of the Software.
#
# THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
# IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
# FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
# AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
# LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
# OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN
# THE SOFTWARE.
#

from __future__ import with_statement
import code
import codecs
import inspect
import os
import pydoc
import re
import rlcompleter
import sys
import textwrap
import traceback
from glob import glob
from itertools import takewhile
from locale import getpreferredencoding
from socket import error as SocketError
from string import Template
from urllib import quote as urlquote
from xmlrpclib import ServerProxy, Error as XMLRPCError

from pygments.lexers import PythonLexer
from pygments.token import Token

from bpython import importcompletion, inspection
from bpython.formatter import Parenthesis

# Needed for special handling of __abstractmethods__
# abc only exists since 2.6, so check both that it exists and that it's
# the one we're expecting
try:
    import abc
    abc.ABCMeta
    has_abc = True
except (ImportError, AttributeError):
    has_abc = False

py3 = sys.version_info[0] == 3


class Interpreter(code.InteractiveInterpreter):

    def __init__(self, locals=None, encoding=None):
        """The syntaxerror callback can be set at any time and will be called
        on a caught syntax error. The purpose for this in bpython is so that
        the repl can be instantiated after the interpreter (which it
        necessarily must be with the current factoring) and then an exception
        callback can be added to the Interpeter instance afterwards - more
        specifically, this is so that autoindentation does not occur after a
        traceback."""

        self.encoding = encoding or sys.getdefaultencoding()
        self.syntaxerror_callback = None
# Unfortunately code.InteractiveInterpreter is a classic class, so no super()
        code.InteractiveInterpreter.__init__(self, locals)

    if not py3:

        def runsource(self, source, filename='<input>', symbol='single',
                      encode=True):
            if encode:
                source = '# coding: %s\n%s' % (self.encoding,
                                               source.encode(self.encoding))
            return code.InteractiveInterpreter.runsource(self, source,
                                                         filename, symbol)

    def showsyntaxerror(self, filename=None):
        """Override the regular handler, the code's copied and pasted from
        code.py, as per showtraceback, but with the syntaxerror callback called
        and the text in a pretty colour."""
        if self.syntaxerror_callback is not None:
            self.syntaxerror_callback()

        type, value, sys.last_traceback = sys.exc_info()
        sys.last_type = type
        sys.last_value = value
        if filename and type is SyntaxError:
            # Work hard to stuff the correct filename in the exception
            try:
                msg, (dummy_filename, lineno, offset, line) = value.args
            except:
                # Not the format we expect; leave it alone
                pass
            else:
                # Stuff in the right filename and right lineno
                if not py3:
                    lineno -= 1
                value = SyntaxError(msg, (filename, lineno, offset, line))
                sys.last_value = value
        list = traceback.format_exception_only(type, value)
        self.writetb(list)

    def showtraceback(self):
        """This needs to override the default traceback thing
        so it can put it into a pretty colour and maybe other
        stuff, I don't know"""
        try:
            t, v, tb = sys.exc_info()
            sys.last_type = t
            sys.last_value = v
            sys.last_traceback = tb
            tblist = traceback.extract_tb(tb)
            del tblist[:1]
            # Set the right lineno (encoding header adds an extra line)
            if not py3:
                for i, (filename, lineno, module, something) in enumerate(tblist):
                    if filename == '<input>':
                        tblist[i] = (filename, lineno - 1, module, something)

            l = traceback.format_list(tblist)
            if l:
                l.insert(0, "Traceback (most recent call last):\n")
            l[len(l):] = traceback.format_exception_only(t, v)
        finally:
            tblist = tb = None

        self.writetb(l)

    def writetb(self, lines):
        """This outputs the traceback and should be overridden for anything
        fancy."""
        for line in lines:
            self.write(line)


class History(object):

    def __init__(self, entries=None):
        if entries is None:
            self.entries = ['']
        else:
            self.entries = list(entries)
        self.index = 0
        self.saved_line = ''

    def append(self, line):
        line = line.rstrip('\n')
        if line:
            self.entries.append(line)

    def first(self):
        """Move back to the beginning of the history."""
        if not self.is_at_end:
            self.index = len(self.entries)
        return self.entries[-self.index]

    def back(self):
        """Move one step back in the history."""
        if not self.is_at_end:
            self.index += 1
        return self.entries[-self.index]

    def forward(self):
        """Move one step forward in the history."""
        if self.index > 1:
            self.index -= 1
            return self.entries[-self.index]
        else:
            self.index = 0
            return self.saved_line

    def last(self):
        """Move forward to the end of the history."""
        if not self.is_at_start:
            self.index = 0
        return self.entries[0]

    @property
    def is_at_end(self):
        return self.index >= len(self.entries) or self.index == -1

    @property
    def is_at_start(self):
        return self.index == 0

    def enter(self, line):
        if self.index == 0:
            self.saved_line = line

    @classmethod
    def from_filename(cls, filename):
        history = cls()
        history.load(filename)
        return history

    def load(self, filename, encoding):
        with codecs.open(filename, 'r', encoding, 'ignore') as hfile:
            for line in hfile:
                self.append(line)

    def reset(self):
        self.index = 0
        self.saved_line = ''

    def save(self, filename, encoding, lines=0):
        with codecs.open(filename, 'w', encoding, 'ignore') as hfile:
            for line in self.entries[-lines:]:
                hfile.write(line)
                hfile.write('\n')


class MatchesIterator(object):

    def __init__(self, current_word='', matches=[]):
        self.current_word = current_word
        self.matches = list(matches)
        self.index = -1

    def __nonzero__(self):
        return self.index != -1

    def __iter__(self):
        return self

    def current(self):
        if self.index == -1:
            raise ValueError('No current match.')
        return self.matches[self.index]

    def next(self):
        self.index = (self.index + 1) % len(self.matches)
        return self.matches[self.index]

    def previous(self):
        if self.index <= 0:
            self.index = len(self.matches)
        self.index -= 1

        return self.matches[self.index]

    def update(self, current_word='', matches=[]):
        if current_word != self.current_word:
            self.current_word = current_word
            self.matches = list(matches)
            self.index = -1


class Interaction(object):
    def __init__(self, config, statusbar=None):
        self.config = config

        if statusbar:
            self.statusbar = statusbar

    def confirm(self, s):
        raise NotImplementedError

    def notify(self, s, n=10):
        raise NotImplementedError

    def file_prompt(self, s):
        raise NotImplementedError


class Repl(object):
    """Implements the necessary guff for a Python-repl-alike interface

    The execution of the code entered and all that stuff was taken from the
    Python code module, I had to copy it instead of inheriting it, I can't
    remember why. The rest of the stuff is basically what makes it fancy.

    It reads what you type, passes it to a lexer and highlighter which
    returns a formatted string. This then gets passed to echo() which
    parses that string and prints to the curses screen in appropriate
    colours and/or bold attribute.

    The Repl class also keeps two stacks of lines that the user has typed in:
    One to be used for the undo feature. I am not happy with the way this
    works.  The only way I have been able to think of is to keep the code
    that's been typed in in memory and re-evaluate it in its entirety for each
    "undo" operation. Obviously this means some operations could be extremely
    slow.  I'm not even by any means certain that this truly represents a
    genuine "undo" implementation, but it does seem to be generally pretty
    effective.

    If anyone has any suggestions for how this could be improved, I'd be happy
    to hear them and implement it/accept a patch. I researched a bit into the
    idea of keeping the entire Python state in memory, but this really seems
    very difficult (I believe it may actually be impossible to work) and has
    its own problems too.

    The other stack is for keeping a history for pressing the up/down keys
    to go back and forth between lines.

    XXX Subclasses should implement echo, current_line, cw
    """

    def __init__(self, interp, config):
        """Initialise the repl.

        interp is a Python code.InteractiveInterpreter instance

        config is a populated bpython.config.Struct.
        """

        self.config = config
        self.cut_buffer = ''
        self.buffer = []
        self.interp = interp
        self.interp.syntaxerror_callback = self.clear_current_line
        self.match = False
        self.rl_history = History()
        self.s_hist = []
        self.history = []
        self.evaluating = False
# Use the interpreter's namespace only for the readline stuff:
        self.completer = rlcompleter.Completer(self.interp.locals)
        self.completer.attr_matches = self.attr_matches
        # Gna, Py 2.6's rlcompleter searches for __call__ inside the
        # instance instead of the type, so we monkeypatch to prevent
        # side-effects (__getattr__/__getattribute__)
        self.completer._callable_postfix = self._callable_postfix
        self.matches = []
        self.matches_iter = MatchesIterator()
        self.argspec = None
        self.current_func = None
        self.highlighted_paren = None
        self.list_win_visible = False
        self._C = {}
        self.prev_block_finished = 0
        self.interact = Interaction(self.config)
        # previous pastebin content to prevent duplicate pastes, filled on call
        # to repl.pastebin
        self.prev_pastebin_content = ''
        self.prev_pastebin_url = ''
        # Necessary to fix mercurial.ui.ui expecting sys.stderr to have this
        # attribute
        self.closed = False

        pythonhist = os.path.expanduser(self.config.hist_file)
        if os.path.exists(pythonhist):
            self.rl_history.load(pythonhist, getpreferredencoding())

    def startup(self):
        """
        Execute PYTHONSTARTUP file if it exits. Call this after front
        end-specific initialisation.
        """
        filename = os.environ.get('PYTHONSTARTUP')
        if filename and os.path.isfile(filename):
            with open(filename, 'r') as f:
                if py3:
                    self.interp.runsource(f.read(), filename, 'exec')
                else:
                    self.interp.runsource(f.read(), filename, 'exec', encode=False)

    def attr_matches(self, text):
        """Taken from rlcompleter.py and bent to my will."""

        m = re.match(r"(\w+(\.\w+)*)\.(\w*)", text)
        if not m:
            return []

        expr, attr = m.group(1, 3)
        if expr.isdigit():
            # Special case: float literal, using attrs here will result in
            # a SyntaxError
            return []
        obj = eval(expr, self.interp.locals)
        with inspection.AttrCleaner(obj):
            matches = self.attr_lookup(obj, expr, attr)
        return matches

    def attr_lookup(self, obj, expr, attr):
        """Second half of original attr_matches method factored out so it can
        be wrapped in a safe try/finally block in case anything bad happens to
        restore the original __getattribute__ method."""
        words = dir(obj)
        if hasattr(obj, '__class__'):
            words.append('__class__')
            words = words + rlcompleter.get_class_members(obj.__class__)
            if has_abc and not isinstance(obj.__class__, abc.ABCMeta):
                try:
                    words.remove('__abstractmethods__')
                except ValueError:
                    pass

        matches = []
        n = len(attr)
        for word in words:
            if word[:n] == attr and word != "__builtins__":
                matches.append("%s.%s" % (expr, word))
        return matches

    def _callable_postfix(self, value, word):
        """rlcompleter's _callable_postfix done right."""
        with inspection.AttrCleaner(value):
            if inspection.is_callable(value):
                word += '('
        return word

    def current_string(self, concatenate=False):
        """Return the current string."""
        tokens = self.tokenize(self.current_line())
        string_tokens = list(takewhile(token_is_any_of([Token.String,
                                                        Token.Text]),
                                       reversed(tokens)))
        if not string_tokens:
            return ''
        opening = string_tokens.pop()[1]
        string = list()
        for (token, value) in reversed(string_tokens):
            if token is Token.Text:
                continue
            elif opening is None:
                opening = value
            elif token is Token.String.Doc:
                string.append(value[3:-3])
                opening = None
            elif value == opening:
                opening = None
                if not concatenate:
                    string = list()
            else:
                string.append(value)

        if opening is None:
            return ''
        return ''.join(string)

    def get_object(self, name):
        attributes = name.split('.')
        obj = eval(attributes.pop(0), self.interp.locals)
        while attributes:
            with inspection.AttrCleaner(obj):
                obj = getattr(obj, attributes.pop(0))
        return obj

    def get_args(self):
        """Check if an unclosed parenthesis exists, then attempt to get the
        argspec() for it. On success, update self.argspec and return True,
        otherwise set self.argspec to None and return False"""

        self.current_func = None

        if not self.config.arg_spec:
            return False

        # Get the name of the current function and where we are in
        # the arguments
        stack = [['', 0, '']]
        try:
            for (token, value) in PythonLexer().get_tokens(
                self.current_line()):
                if token is Token.Punctuation:
                    if value in '([{':
                        stack.append(['', 0, value])
                    elif value in ')]}':
                        stack.pop()
                    elif value == ',':
                        try:
                            stack[-1][1] += 1
                        except TypeError:
                            stack[-1][1] = ''
                elif (token is Token.Name or token in Token.Name.subtypes or
                      token is Token.Operator and value == '.'):
                    stack[-1][0] += value
                elif token is Token.Operator and value == '=':
                    stack[-1][1] = stack[-1][0]
                else:
                    stack[-1][0] = ''
            while stack[-1][2] in '[{':
                stack.pop()
            _, arg_number, _ = stack.pop()
            func, _, _ = stack.pop()
        except IndexError:
            return False
        if not func:
            return False

        try:
            f = self.get_object(func)
        except (AttributeError, NameError, SyntaxError):
            return False

        if inspect.isclass(f):
            try:
                if f.__init__ is not object.__init__:
                    f = f.__init__
            except AttributeError:
                return None
        self.current_func = f

        self.argspec = inspection.getargspec(func, f)
        if self.argspec:
            self.argspec.append(arg_number)
            return True
        return False

    def get_source_of_current_name(self):
        """Return the source code of the object which is bound to the
        current name in the current input line. Return `None` if the
        source cannot be found."""
        try:
            obj = self.current_func
            if obj is None:
                line = self.current_line()
                if inspection.is_eval_safe_name(line):
                    obj = self.get_object(line)
            source = inspect.getsource(obj)
        except (AttributeError, IOError, NameError, TypeError):
            return None
        else:
            return source

    def complete(self, tab=False):
        """Construct a full list of possible completions and construct and
        display them in a window. Also check if there's an available argspec
        (via the inspect module) and bang that on top of the completions too.
        The return value is whether the list_win is visible or not."""

        self.docstring = None
        if not self.get_args():
            self.argspec = None
        elif self.current_func is not None:
            self.docstring = pydoc.getdoc(self.current_func)
            if not self.docstring:
                self.docstring = None

        cw = self.cw()
        cs = self.current_string()
        if not cw:
            self.matches = []
            self.matches_iter.update()
        if not (cw or cs):
            return bool(self.argspec)

        if cs and tab:
            # Filename completion
            self.matches = list()
            username = cs.split(os.path.sep, 1)[0]
            user_dir = os.path.expanduser(username)
            for filename in glob(os.path.expanduser(cs + '*')):
                if os.path.isdir(filename):
                    filename += os.path.sep
                if cs.startswith('~'):
                    filename = username + filename[len(user_dir):]
                self.matches.append(filename)
            self.matches_iter.update(cs, self.matches)
            return bool(self.matches)
        elif cs:
            # Do not provide suggestions inside strings, as one cannot tab
            # them so they would be really confusing.
            self.matches_iter.update()
            return False

        # Check for import completion
        e = False
        matches = importcompletion.complete(self.current_line(), cw)
        if matches is not None and not matches:
            self.matches = []
            self.matches_iter.update()
            return False

        if matches is None:
            # Nope, no import, continue with normal completion
            try:
                self.completer.complete(cw, 0)
            except Exception:
# This sucks, but it's either that or list all the exceptions that could
# possibly be raised here, so if anyone wants to do that, feel free to send me
# a patch. XXX: Make sure you raise here if you're debugging the completion
# stuff !
                e = True
            else:
                matches = self.completer.matches

        if not e and self.argspec:
            matches.extend(name + '=' for name in self.argspec[1][0]
                           if name.startswith(cw))
            if py3:
                matches.extend(name + '=' for name in self.argspec[1][4]
                               if name.startswith(cw))

# unless the first character is a _ filter out all attributes starting with a _
        if not e and not cw.split('.')[-1].startswith('_'):
            matches = [match for match in matches
                       if not match.split('.')[-1].startswith('_')]

        if e or not matches:
            self.matches = []
            self.matches_iter.update()
            if not self.argspec:
                return False
        else:
# remove duplicates
            self.matches = sorted(set(matches))


        if len(self.matches) == 1 and not self.config.auto_display_list:
            self.list_win_visible = True
            self.tab()
            return False

        self.matches_iter.update(cw, self.matches)
        return True

    def format_docstring(self, docstring, width, height):
        """Take a string and try to format it into a sane list of strings to be
        put into the suggestion box."""

        lines = docstring.split('\n')
        out = []
        i = 0
        for line in lines:
            i += 1
            if not line.strip():
                out.append('\n')
            for block in textwrap.wrap(line, width):
                out.append('  ' + block + '\n')
                if i >= height:
                    return out
                i += 1
        # Drop the last newline
        out[-1] = out[-1].rstrip()
        return out

    def next_indentation(self):
        """Return the indentation of the next line based on the current
        input buffer."""
        if self.buffer:
            indentation = next_indentation(self.buffer[-1],
                                           self.config.tab_length)
            if indentation and self.config.dedent_after > 0:
                line_is_empty = lambda line: not line.strip()
                empty_lines = takewhile(line_is_empty, reversed(self.buffer))
                if sum(1 for _ in empty_lines) >= self.config.dedent_after:
                    indentation -= 1
        else:
            indentation = 0
        return indentation

    def formatforfile(self, s):
        """Format the stdout buffer to something suitable for writing to disk,
        i.e. without >>> and ... at input lines and with "# OUT: " prepended to
        output lines."""

        def process():
            for line in s.split('\n'):
                if line.startswith('>>>') or line.startswith('...'):
                    yield line[4:]
                elif line.rstrip():
                    yield "# OUT: %s" % (line,)
        return "\n".join(process())

    def write2file(self):
        """Prompt for a filename and write the current contents of the stdout
        buffer to disk."""

        try:
            fn = self.interact.file_prompt('Save to file (Esc to cancel): ')
            if not fn:
                self.interact.notify("Save cancelled.")
                return
        except ValueError:
            self.interact.notify("Save cancelled.")
            return

        if fn.startswith('~'):
            fn = os.path.expanduser(fn)

        s = self.formatforfile(self.getstdout())

        try:
            f = open(fn, 'w')
            f.write(s)
            f.close()
        except IOError:
            self.interact.notify("Disk write error for file '%s'." % (fn, ))
        else:
            self.interact.notify('Saved to %s' % (fn, ))

    def pastebin(self, s=None):
        """Upload to a pastebin and display the URL in the status bar."""

        if s is None:
            s = self.getstdout()

        if (self.config.pastebin_confirm and
            not self.interact.confirm("Pastebin buffer? (y/N) ")):
            self.interact.notify("Pastebin aborted")
            return

        pasteservice = ServerProxy(self.config.pastebin_url)

        if s == self.prev_pastebin_content:
            self.interact.notify('Duplicate pastebin. Previous URL: ' +
                                  self.prev_pastebin_url)
            return self.prev_pastebin_url

        self.prev_pastebin_content = s

        self.interact.notify('Posting data to pastebin...')
        try:
            paste_id = pasteservice.pastes.newPaste('pycon', s)
        except (SocketError, XMLRPCError), e:
            self.interact.notify('Upload failed: %s' % (str(e), ) )
            return

        paste_url_template = Template(self.config.pastebin_show_url)
        paste_id = urlquote(paste_id)
        paste_url = paste_url_template.safe_substitute(paste_id=paste_id)
        self.prev_pastebin_url = paste_url
        self.interact.notify('Pastebin URL: %s' % (paste_url, ), 10)
        return paste_url

    def push(self, s, insert_into_history=True):
        """Push a line of code onto the buffer so it can process it all
        at once when a code block ends"""
        s = s.rstrip('\n')
        self.buffer.append(s)

        if insert_into_history:
            self.rl_history.append(s)

        more = self.interp.runsource('\n'.join(self.buffer))

        if not more:
            self.buffer = []

        return more

    def undo(self, n=1):
        """Go back in the undo history n steps and call reeavluate()
        Note that in the program this is called "Rewind" because I
        want it to be clear that this is by no means a true undo
        implementation, it is merely a convenience bonus."""
        if not self.history:
            return None

        if len(self.history) < n:
            n = len(self.history)

        entries = list(self.rl_history.entries)

        self.history = self.history[:-n]

        self.reevaluate()

        self.rl_history.entries = entries

    def flush(self):
        """Olivier Grisel brought it to my attention that the logging
        module tries to call this method, since it makes assumptions
        about stdout that may not necessarily be true. The docs for
        sys.stdout say:

        "stdout and stderr needn't be built-in file objects: any
         object is acceptable as long as it has a write() method
         that takes a string argument."

        So I consider this to be a bug in logging, and this is a hack
        to fix it, unfortunately. I'm sure it's not the only module
        to do it."""

    def close(self):
        """See the flush() method docstring."""

    def tokenize(self, s, newline=False):
        """Tokenize a line of code."""

        source = '\n'.join(self.buffer + [s])
        cursor = len(source) - self.cpos
        if self.cpos:
            cursor += 1
        stack = list()
        all_tokens = list(PythonLexer().get_tokens(source))
        # Unfortunately, Pygments adds a trailing newline and strings with
        # no size, so strip them
        while not all_tokens[-1][1]:
            all_tokens.pop()
        all_tokens[-1] = (all_tokens[-1][0], all_tokens[-1][1].rstrip('\n'))
        line = pos = 0
        parens = dict(zip('{([', '})]'))
        line_tokens = list()
        saved_tokens = list()
        search_for_paren = True
        for (token, value) in split_lines(all_tokens):
            pos += len(value)
            if token is Token.Text and value == '\n':
                line += 1
                # Remove trailing newline
                line_tokens = list()
                saved_tokens = list()
                continue
            line_tokens.append((token, value))
            saved_tokens.append((token, value))
            if not search_for_paren:
                continue
            under_cursor = (pos == cursor)
            if token is Token.Punctuation:
                if value in parens:
                    if under_cursor:
                        line_tokens[-1] = (Parenthesis.UnderCursor, value)
                        # Push marker on the stack
                        stack.append((Parenthesis, value))
                    else:
                        stack.append((line, len(line_tokens) - 1,
                                      line_tokens, value))
                elif value in parens.itervalues():
                    saved_stack = list(stack)
                    try:
                        while True:
                            opening = stack.pop()
                            if parens[opening[-1]] == value:
                                break
                    except IndexError:
                        # SyntaxError.. more closed parentheses than
                        # opened or a wrong closing paren
                        opening = None
                        if not saved_stack:
                            search_for_paren = False
                        else:
                            stack = saved_stack
                    if opening and opening[0] is Parenthesis:
                        # Marker found
                        line_tokens[-1] = (Parenthesis, value)
                        search_for_paren = False
                    elif opening and under_cursor and not newline:
                        if self.cpos:
                            line_tokens[-1] = (Parenthesis.UnderCursor, value)
                        else:
                            # The cursor is at the end of line and next to
                            # the paren, so it doesn't reverse the paren.
                            # Therefore, we insert the Parenthesis token
                            # here instead of the Parenthesis.UnderCursor
                            # token.
                            line_tokens[-1] = (Parenthesis, value)
                        (lineno, i, tokens, opening) = opening
                        if lineno == len(self.buffer):
                            self.highlighted_paren = (lineno, saved_tokens)
                            line_tokens[i] = (Parenthesis, opening)
                        else:
                            self.highlighted_paren = (lineno, list(tokens))
                            # We need to redraw a line
                            tokens[i] = (Parenthesis, opening)
                            self.reprint_line(lineno, tokens)
                        search_for_paren = False
                elif under_cursor:
                    search_for_paren = False
        if line != len(self.buffer):
            return list()
        return line_tokens

    def clear_current_line(self):
        """This is used as the exception callback for the Interpreter instance.
        It prevents autoindentation from occuring after a traceback."""


def next_indentation(line, tab_length):
    """Given a code line, return the indentation of the next line."""
    line = line.expandtabs(tab_length)
    indentation = (len(line) - len(line.lstrip(' '))) // tab_length
    if line.rstrip().endswith(':'):
        indentation += 1
    return indentation


def next_token_inside_string(s, inside_string):
    """Given a code string s and an initial state inside_string, return
    whether the next token will be inside a string or not."""
    for token, value in PythonLexer().get_tokens(s):
        if token is Token.String:
            value = value.lstrip('bBrRuU')
            if value in ['"""', "'''", '"', "'"]:
                if not inside_string:
                    inside_string = value
                elif value == inside_string:
                    inside_string = False
    return inside_string


def split_lines(tokens):
    for (token, value) in tokens:
        if not value:
            continue
        while value:
            head, newline, value = value.partition('\n')
            yield (token, head)
            if newline:
                yield (Token.Text, newline)


def token_is(token_type):
    """Return a callable object that returns whether a token is of the
    given type `token_type`."""

    def token_is_type(token):
        """Return whether a token is of a certain type or not."""
        token = token[0]
        while token is not token_type and token.parent:
            token = token.parent
        return token is token_type

    return token_is_type


def token_is_any_of(token_types):
    """Return a callable object that returns whether a token is any of the
    given types `token_types`."""
    is_token_types = map(token_is, token_types)

    def token_is_any_of(token):
        return any(check(token) for check in is_token_types)

    return token_is_any_of
