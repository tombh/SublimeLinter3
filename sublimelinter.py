#
# sublimelinter.py
# Part of SublimeLinter3, a code checking framework for Sublime Text 3
#
# Written by Ryan Hileman and Aparajita Fishman
#
# Project: https://github.com/SublimeLinter/SublimeLinter3
# License: MIT
#

"""This module provides the SublimeLinter plugin class and supporting methods."""

import os
import re

import sublime
import sublime_plugin

from .lint.linter import Linter
from .lint.highlight import HighlightSet
from .lint.queue import queue
from .lint import persist, util


def plugin_loaded():
    """The ST3 entry point for plugins."""

    persist.plugin_is_loaded = True
    persist.settings.load()

    plugin = SublimeLinter.shared_plugin()
    queue.start(plugin.lint)

    util.generate_menus()
    util.generate_color_scheme(from_reload=False)
    util.install_syntaxes()

    persist.settings.on_update_call(SublimeLinter.on_settings_updated)

    # This ensures we lint the active view on a fresh install
    window = sublime.active_window()

    if window:
        plugin.on_activated(window.active_view())


class SublimeLinter(sublime_plugin.EventListener):

    """The main ST3 plugin class."""

    # We use this to match linter settings filenames.
    LINTER_SETTINGS_RE = re.compile('^SublimeLinter(-.+?)?\.sublime-settings')

    shared_instance = None

    @classmethod
    def shared_plugin(cls):
        """Return the plugin instance."""
        return cls.shared_instance

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

        # Keeps track of which views we have assigned linters to
        self.loaded_views = set()

        # Keeps track of which views have actually been linted
        self.linted_views = set()

        # A mapping between view ids and syntax names
        self.view_syntax = {}

        self.__class__.shared_instance = self

    @classmethod
    def lint_all_views(cls):
        """Simulate a modification of all views, which will trigger a relint."""

        def apply(view):
            if view.id() in persist.view_linters:
                cls.shared_instance.hit(view)

        util.apply_to_all_views(apply)

    def lint(self, view_id, hit_time=None, callback=None):
        """
        Lint the view with the given id.

        This method is called asynchronously by persist.Daemon when a lint
        request is pulled off the queue.

        If provided, hit_time is the time at which the lint request was added
        to the queue. It is used to determine if the view has been modified
        since the lint request was queued. If so, the lint is aborted, since
        another lint request is already in the queue.

        callback is the method to call when the lint is finished. If not
        provided, it defaults to highlight().

        """

        # If the view has been modified since the lint was triggered,
        # don't lint again.
        if hit_time is not None and persist.last_hit_times.get(view_id, 0) > hit_time:
            return

        view = Linter.get_view(view_id)

        if view is None:
            return

        # Build a list of regions that match the linter's selectors
        sections = {}

        for sel, _ in Linter.get_selectors(view_id):
            sections[sel] = []

            for region in view.find_by_selector(sel):
                sections[sel].append((view.rowcol(region.a)[0], region.a, region.b))

        filename = view.file_name()
        code = Linter.text(view)
        callback = callback or self.highlight
        Linter.lint_view(view_id, filename, code, sections, hit_time, callback)

    def highlight(self, view, linters, hit_time):
        """
        Highlight any errors found during a lint of the given view.

        This method is called by Linter.lint_view after linting is finished.

        linters is a list of the linters that ran. hit_time has the same meaning
        as in lint(), and if the view was modified since the lint request was
        made, this method aborts drawing marks.

        If the view has not been modified since hit_time, all of the marks and
        errors from the list of linters are aggregated and drawn, and the status
        is updated.

        """

        vid = view.id()

        # If the view has been modified since the lint was triggered,
        # don't draw marks.
        if hit_time is not None and persist.last_hit_times.get(vid, 0) > hit_time:
            return

        errors = {}
        highlights = persist.highlights[vid] = HighlightSet()

        for linter in linters:
            if linter.highlight:
                highlights.add(linter.highlight)

            if linter.errors:
                for line, errs in linter.errors.items():
                    errors.setdefault(line, []).extend(errs)

        highlights.clear(view)
        highlights.draw(view)
        persist.errors[vid] = errors

        # Update the status
        self.on_selection_modified_async(view)

    def hit(self, view):
        """Record an activity that could trigger a lint and enqueue a desire to lint."""

        vid = view.id()
        self.check_syntax(view)
        self.linted_views.add(vid)

        if view.size() == 0:
            for linter in Linter.get_linters(vid):
                linter.clear()

            return

        persist.last_hit_times[vid] = queue.hit(view)

    def check_syntax(self, view):
        """
        Check and return if view's syntax has changed.

        If the syntax has changed, a new linter is assigned.

        """

        vid = view.id()
        syntax = persist.get_syntax(view)

        # Syntax either has never been set or just changed
        if not vid in self.view_syntax or self.view_syntax[vid] != syntax:
            self.view_syntax[vid] = syntax
            Linter.assign(view, reset=True)
            self.clear(view)
            return True
        else:
            return False

    def clear(self, view):
        """Clear all marks, errors and status from the given view."""
        Linter.clear_view(view)

    # sublime_plugin.EventListener event handlers

    def on_modified(self, view):
        """Called when a view is modified."""

        if view.is_scratch():
            return

        if view.id() not in persist.view_linters:
            syntax_changed = self.check_syntax(view)

            if not syntax_changed:
                return
        else:
            syntax_changed = False

        if syntax_changed or persist.settings.get('lint_mode') == 'background':
            self.hit(view)
        else:
            self.clear(view)

    def on_activated(self, view):
        """Called when a view gains input focus."""

        if view.is_scratch():
            return

        # Reload the plugin settings.
        persist.settings.load()

        self.check_syntax(view)
        view_id = view.id()

        if not view_id in self.linted_views:
            if not view_id in self.loaded_views:
                self.on_new(view)

            if persist.settings.get('lint_mode') in ('background', 'load/save'):
                self.hit(view)

        self.on_selection_modified_async(view)

    def on_open_settings(self, view):
        """
        Called when any settings file is opened.

        view is the view that contains the text of the settings file.

        """
        if self.is_settings_file(view, user_only=True):
            persist.settings.save(view=view)

    def is_settings_file(self, view, user_only=False):
        """Return True if view is a SublimeLinter settings file."""

        filename = view.file_name()

        if not filename:
            return False

        dirname, filename = os.path.split(filename)
        dirname = os.path.basename(dirname)

        if self.LINTER_SETTINGS_RE.match(filename):
            if user_only:
                return dirname == 'User'
            else:
                return dirname in (persist.PLUGIN_DIRECTORY, 'User')

    @classmethod
    def on_settings_updated(cls, relint=False):
        """Callback triggered when the settings are updated."""
        if relint:
            cls.lint_all_views()
        else:
            Linter.redraw_all()

    def on_new(self, view):
        """Called when a new buffer is created."""
        self.on_open_settings(view)

        if view.is_scratch():
            return

        vid = view.id()
        self.loaded_views.add(vid)
        self.view_syntax[vid] = persist.get_syntax(view)

    def on_selection_modified_async(self, view):
        """Called when the selection changes (cursor moves or text selected)."""

        if view.is_scratch():
            return

        vid = view.id()

        # Get the line number of the first line of the first selection.
        try:
            lineno = view.rowcol(view.sel()[0].begin())[0]
        except IndexError:
            lineno = -1

        if vid in persist.errors:
            errors = persist.errors[vid]

            if errors:
                lines = sorted(list(errors))
                counts = [len(errors[line]) for line in lines]
                count = sum(counts)
                plural = 's' if count > 1 else ''

                if lineno in errors:
                    # Sort the errors by column
                    line_errors = sorted(errors[lineno], key=lambda error: error[0])
                    line_errors = [error[1] for error in line_errors]

                    if plural:
                        # Sum the errors before the first error on this line
                        index = lines.index(lineno)
                        first = sum(counts[0:index]) + 1

                        if len(line_errors) > 1:
                            last = first + len(line_errors) - 1
                            status = '{}-{} of {} errors: '.format(first, last, count)
                        else:
                            status = '{} of {} errors: '.format(first, count)
                    else:
                        status = 'Error: '

                    status += '; '.join(line_errors)
                else:
                    status = '%i error%s' % (count, plural)

                view.set_status('sublimelinter', status)
            else:
                view.erase_status('sublimelinter')

    def on_pre_save(self, view):
        """
        Called before view is saved.

        If a settings file is the active view and is saved,
        copy the current settings first so we can compare post-save.

        """
        if view.window().active_view() == view and self.is_settings_file(view):
            persist.settings.copy()

    def on_post_save(self, view):
        """Called after view is saved."""

        if view.is_scratch():
            return

        # First check to see if the project settings changed
        if view.window().project_file_name() == view.file_name():
            self.lint_all_views()
        else:
            # Now see if a .sublimelinterrc has changed
            filename = os.path.basename(view.file_name())

            if filename == '.sublimelinterrc':
                # If it's the main .sublimelinterrc, reload the settings
                rc_path = os.path.join(os.path.dirname(__file__), '.sublimelinterrc')

                if view.file_name() == rc_path:
                    persist.settings.load(force=True)
                else:
                    self.lint_all_views()

            # If a file other than one of our settings files changed,
            # check if the syntax changed or if we need to show errors.
            elif filename != 'SublimeLinter.sublime-settings':
                syntax_changed = self.check_syntax(view)
                vid = view.id()
                mode = persist.settings.get('lint_mode')
                show_errors = persist.settings.get('show_errors_on_save')

                if syntax_changed:
                    self.clear(view)

                    if vid in persist.view_linters:
                        if mode != 'manual':
                            self.lint(vid)
                        else:
                            show_errors = False
                    else:
                        show_errors = False
                else:
                    if show_errors or mode in ('load/save', 'save only'):
                        self.lint(vid)
                    elif mode == 'manual':
                        show_errors = False

                if show_errors and vid in persist.errors and persist.errors[vid]:
                    view.run_command('sublimelinter_show_all_errors')

    def on_close(self, view):
        """Called after view is closed."""

        if view.is_scratch():
            return

        vid = view.id()

        if vid in self.loaded_views:
            self.loaded_views.remove(vid)

        if vid in self.linted_views:
            self.linted_views.remove(vid)

        if vid in self.view_syntax:
            del self.view_syntax[vid]

        persist.view_did_close(vid)


class SublimelinterEditCommand(sublime_plugin.TextCommand):

    """A plugin command used to generate an edit object for a view."""

    def run(self, edit):
        """Run the command."""
        persist.edit(self.view.id(), edit)
