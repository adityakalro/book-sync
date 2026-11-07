"""Independent catalog discovery and progress jobs for the plugin registry."""
from collections import Counter

from book_sync.errors import ReadFailure
from book_sync.sync.engine import sync_book


def refresh_libraries(root, plugins):
    counts = Counter()
    for key, plugin in plugins.items():
        if plugin.discover is None or not all(k in plugins for k in plugin.catalog_requires):
            continue
        try:
            counts.update(plugin.discover(root, set(plugins)))
        except Exception:
            counts['discovery_error'] += 1
    return dict(counts)


def run_books(root, plugins, notify, *, dry_run=False):
    results = []
    seen = set()
    for key, plugin in plugins.items():
        if not plugin.catalog_ready(plugins):
            continue
        try:
            for book in plugin.books(root):
                if isinstance(book, ReadFailure):
                    results.append(error_result(plugin.label, str(book)))
                    continue
                if book.key in seen:
                    continue
                seen.add(book.key)
                notify('Syncing ' + book.title + '…')
                try:
                    result = sync_book(root, book, plugins, dry_run=dry_run)
                except Exception as error:
                    message = str(error) if isinstance(error, ReadFailure) else 'Could not open the book or a connected reader.'
                    result = error_result(book.title, message)
                if result.get('source') in plugins:
                    source = result['source']
                    result['message'] = result['message'].replace(source + ' →', plugins[source].label + ' →', 1)
                results.append({'title': book.title, **result})
        except Exception:
            results.append(error_result(plugin.label, 'Could not load the book catalog.'))
    return results


def error_result(title, message):
    return {'title': title, 'status': 'error', 'writes': 0, 'message': message}
