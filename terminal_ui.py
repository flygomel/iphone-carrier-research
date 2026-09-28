"""Small terminal presentation layer; no device operations."""
import os
import sys


class TerminalUI:
    def __init__(self, stream=None):
        self.stream = stream or sys.stdout
        self.terminal = self.stream.isatty() and os.environ.get('TERM') != 'dumb'
        self.color = self.terminal and 'NO_COLOR' not in os.environ
        self.pending = False

    def styled(self, text, code):
        return f'\033[{code}m{text}\033[0m' if self.color else text

    def clear(self):
        if self.pending and self.terminal:
            self.stream.write('\r\033[2K')
        self.pending = False

    def line(self, text=''):
        self.clear()
        print(text, file=self.stream, flush=True)

    def title(self):
        self.line('\n  '+self.styled('5G для iPhone', '1')+'\n')

    def status(self, text):
        self.clear()
        if self.terminal:
            self.stream.write('  '+self.styled('·', '36')+' '+text)
            self.stream.flush()
            self.pending = True
        else:
            self.line('  · '+text)

    def done(self, text):
        self.line('  '+self.styled('✓', '32')+' '+text)

    def finish(self, text):
        self.line('\n  '+self.styled(text, '1')+'\n')

    def error(self, text):
        self.line('\n  '+self.styled('✗', '31')+' '+text)
