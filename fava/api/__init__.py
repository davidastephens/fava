"""
This module provides the data required by Fava's reports.
"""

import datetime
import operator
import os

from beancount.core.number import Decimal

from beancount import loader
from beancount.core import compare, flags, getters, realization, inventory
from beancount.core.realization import RealAccount
from beancount.core.interpolate import compute_entries_balance
from beancount.core.account_types import get_account_sign
from beancount.core.data import (get_entry, iter_entry_dates, Open, Close,
                                 Note, Document, Balance, TxnPosting,
                                 Transaction, Event, Query, Custom)
from beancount.ops import prices, holdings, summarize
from beancount.parser import options
from beancount.query import query
from beancount.reports import context
from beancount.utils import misc_utils

from fava.util import date
from fava.api.budgets import Budgets
from fava.api.filters import AccountFilter, DateFilter, PayeeFilter, TagFilter
from fava.api.helpers import holdings_at_dates
from fava.api.serialization import (serialize_inventory, serialize_entry,
                                    serialize_entry_with,
                                    serialize_real_account,
                                    zip_real_accounts)


class FavaAPIException(Exception):
    pass


def _filter_entries_by_type(entries, include_types):
    return [entry for entry in entries
            if isinstance(entry, include_types)]


def _list_accounts(root_account, leaf_only=False):
    """List of all sub-accounts of the given root."""
    accounts = [child_account.account
                for child_account in
                realization.iter_children(root_account, leaf_only)]

    return accounts[1:]


def _journal(entries, include_types=None):
    if include_types:
        entries = _filter_entries_by_type(entries, include_types)

    return [serialize_entry(entry) for entry in entries]


def _real_account(account_name, entries, begin_date=None, end_date=None,
                  min_accounts=None):
    """
    Returns the realization.RealAccount instances for account_name, and
    their entries clamped by the optional begin_date and end_date.

    Warning: For efficiency, the returned result does not include any added
    postings to account for balances at 'begin_date'.

    :return: realization.RealAccount instances
    """
    if begin_date:
        entries = list(iter_entry_dates(entries, begin_date, end_date))
    if not min_accounts:
        min_accounts = [account_name]

    return realization.get(realization.realize(entries, min_accounts),
                           account_name)


def _sidebar_links(entries):
    """ Parse custom entries for links. They have the following format:

    2016-04-01 custom "fava-sidebar-link" "2014" "/income_statement/?time=2014"

    """
    sidebar_link_entries = [
        entry for entry in _filter_entries_by_type(entries, Custom)
        if entry.type == 'fava-sidebar-link']
    return [(entry.values[0].value, entry.values[1].value)
            for entry in sidebar_link_entries]


def _settings(entries):
    """ Parse custom entries for fava settings. They have the following format:

    2016-04-01 custom "fava-option" "[setting_name]" [setting_value]

    """
    settings_entries = [
        entry for entry in _filter_entries_by_type(entries, Custom)
        if entry.type == 'fava-option']
    return {entry.values[0].value: entry.values[1].value
            for entry in settings_entries}


class BeancountReportAPI():
    """An instance of this class provides methods to access and filter the
    entries in the given beancount file."""

    def __init__(self, beancount_file_path):
        self.beancount_file_path = beancount_file_path
        self.filters = {
            'time': DateFilter(),
            'tag': TagFilter(),
            'account': AccountFilter(),
            'payee': PayeeFilter(),
        }

        self.load_file()

    def load_file(self):
        """Load self.beancount_file_path and compute things that are independent
        of how the entries might be filtered later"""
        self.all_entries, self.errors, self.options = \
            loader.load_file(self.beancount_file_path)
        self.price_map = prices.build_price_map(self.all_entries)
        self.account_types = options.get_account_types(self.options)

        self.title = self.options['title']
        if self.options['render_commas']:
            self.format_string = '{:,f}'
            self.default_format_string = '{:,.2f}'
        else:
            self.format_string = '{:f}'
            self.default_format_string = '{:.2f}'

        self.active_years = list(getters.get_active_years(self.all_entries))
        self.active_tags = list(getters.get_all_tags(self.all_entries))
        self.active_payees = list(getters.get_all_payees(self.all_entries))

        self.queries = _filter_entries_by_type(self.all_entries, Query)

        self.all_root_account = realization.realize(self.all_entries,
                                                    self.account_types)
        self.all_accounts = _list_accounts(self.all_root_account)
        self.all_accounts_leaf_only = _list_accounts(
            self.all_root_account, leaf_only=True)

        self.sidebar_links = _sidebar_links(self.all_entries)
        self.settings = _settings(self.all_entries)

        self._apply_filters()

        self.budgets = Budgets(self.entries)
        self.errors.extend(self.budgets.errors)

    def _apply_filters(self):
        self.entries = self.all_entries

        for filter_class in self.filters.values():
            self.entries = filter_class.apply(self.entries, self.options)

        self.root_account = realization.realize(self.entries,
                                                self.account_types)

        self.date_first, self.date_last = \
            getters.get_min_max_dates(self.entries, (Transaction))

        if self.filters['time']:
            self.date_first = self.filters['time'].begin_date
            self.date_last = self.filters['time'].end_date

    def filter(self, **kwargs):
        """Set and apply (if necessary) filters."""
        changed = False
        for filter_name, filter_class in self.filters.items():
            if filter_class.set(kwargs[filter_name]):
                changed = True

        if changed:
            self._apply_filters()

    def quantize(self, value, currency):
        """Use display context for the currency to quantize the given value to
        the right number of decimal digits."""
        if not currency:
            return self.default_format_string.format(value)
        return self.format_string.format(
            self.options['dcontext'].quantize(value, currency))

    def _interval_tuples(self, interval):
        """Calculates tuples of (begin_date, end_date) of length interval for the
        period in which entries contains transactions.  """
        return date.interval_tuples(self.date_first, self.date_last, interval)

    def _total_balance(self, names, begin_date, end_date):
        totals = [realization.compute_balance(
            _real_account(account_name, self.entries, begin_date, end_date))
                  for account_name in names]
        return serialize_inventory(sum(totals, inventory.Inventory()),
                                   at_cost=True)

    def interval_totals(self, interval, account_name, accumulate=False):
        """Renders totals for account (or accounts) in the intervals."""
        if isinstance(account_name, str):
            names = [account_name]
        else:
            names = account_name

        interval_tuples = self._interval_tuples(interval)
        return [{
            'begin_date': begin_date,
            'totals': self._total_balance(
                names,
                begin_date if not accumulate else self.date_first, end_date),
        } for begin_date, end_date in interval_tuples]

    def get_account_sign(self, account_name):
        return get_account_sign(account_name, self.account_types)

    def balances(self, account_name, begin_date=None, end_date=None):
        real_account = _real_account(
            account_name, self.entries, begin_date, end_date)
        return [serialize_real_account(real_account)]

    def closing_balances(self, account_name):
        closing_entries = summarize.cap_opt(self.entries, self.options)
        return [serialize_real_account(
            _real_account(account_name, closing_entries))]

    def interval_balances(self, interval, account_name, accumulate=False):
        """accumulate is False for /changes and True for /balances"""
        account_names = [account
                         for account in self.all_accounts
                         if account.startswith(account_name)]

        interval_tuples = self._interval_tuples(interval)
        interval_balances = [
            _real_account(
                account_name, self.entries,
                interval_tuples[0][0] if accumulate else begin_date,
                end_date, min_accounts=account_names)
            for begin_date, end_date in interval_tuples]

        return self.add_budgets(zip_real_accounts(interval_balances),
                                interval_tuples, accumulate), interval_tuples

    def add_budgets(self, zipped_interval_balances, interval_tuples,
                    accumulate):
        """Add budgets data to zipped (recursive) interval balances."""
        if not zipped_interval_balances:
            return

        interval_budgets = [
            self.budgets.budget(
                zipped_interval_balances['account'],
                interval_tuples[0][0] if accumulate else begin_date,
                end_date
            ) for begin_date, end_date in interval_tuples]

        zipped_interval_balances['balance_and_balance_children'] = [(
            balances[0],
            balances[1],
            {curr: value - (balances[0][curr] if curr in balances[0] else Decimal(0.0)) for curr, value in budget.items()},  # noqa
            {curr: value - (balances[1][curr] if curr in balances[1] else Decimal(0.0)) for curr, value in budget.items()})  # noqa
            for balances, budget in zip(zipped_interval_balances['balance_and_balance_children'], interval_budgets)  # noqa
        ]

        zipped_interval_balances['children'] = [self.add_budgets(
            child, interval_tuples, accumulate)
            for child in zipped_interval_balances['children']]

        return zipped_interval_balances

    def trial_balance(self):
        return serialize_real_account(self.root_account)['children']

    def account_journal(self, account_name, with_journal_children=False):
        real_account = realization.get_or_create(self.root_account,
                                                 account_name)

        if with_journal_children:
            postings = realization.get_postings(real_account)
        else:
            postings = real_account.txn_postings

        return [serialize_entry_with(entry, change, balance)
                for entry, _, change, balance in
                realization.iterate_with_balance(postings)]

    def journal(self):
        return _journal(self.entries)

    def documents(self):
        return _journal(self.entries, Document)

    def notes(self):
        return _journal(self.entries, Note)

    def get_query(self, name):
        matching_entries = [query for query in self.queries
                            if name == query.name]

        if not matching_entries:
            return

        assert len(matching_entries) == 1
        return matching_entries[0]

    def events(self, event_type=None, only_include_newest=False):
        events = _journal(self.entries, Event)

        if event_type:
            events = [event for event in events if event['type'] == event_type]

        if only_include_newest:
            seen_types = list()
            for event in events:
                if not event['type'] in seen_types:
                    seen_types.append(event['type'])
            events = list({event['type']: event for event in events}.values())

        return events

    def holdings(self, aggregation_key=None):
        holdings_list = holdings.get_final_holdings(
            self.entries,
            (self.account_types.assets, self.account_types.liabilities),
            self.price_map)

        if aggregation_key:
            holdings_list = holdings.aggregate_holdings_by(
                holdings_list, operator.attrgetter(aggregation_key))
        return holdings_list

    def _holdings_to_net_worth(self, holdings_list):
        totals = {}
        for currency in self.options['operating_currency']:
            currency_holdings_list = \
                holdings.convert_to_currency(self.price_map, currency,
                                             holdings_list)
            if not currency_holdings_list:
                continue

            holdings_ = holdings.aggregate_holdings_by(
                currency_holdings_list,
                operator.attrgetter('cost_currency'))

            holdings_ = [holding
                         for holding in holdings_
                         if holding.currency and holding.cost_currency]

            # If after conversion there are no valid holdings, skip the
            # currency altogether.
            if holdings_:
                totals[currency] = holdings_[0].market_value
        return totals

    def net_worth_at_intervals(self, interval):
        interval_tuples = self._interval_tuples(interval)
        dates = [p[1] for p in interval_tuples]

        return [{
            'date': date,
            'balance': self._holdings_to_net_worth(holdings_list),
        } for date, holdings_list in
            zip(dates, holdings_at_dates(self.entries, dates,
                                         self.price_map, self.options))]

    def net_worth(self, interval='month'):
        return self._holdings_to_net_worth(self.holdings())

    def context(self, ehash):
        matching_entries = [entry for entry in self.all_entries
                            if ehash == compare.hash_entry(entry)]

        if not matching_entries:
            return

        # the hash should uniquely identify the entry
        assert len(matching_entries) == 1
        entry = matching_entries[0]
        context_str = context.render_entry_context(self.all_entries,
                                                   self.options, entry)
        return {
            'hash': ehash,
            'context': context_str.split("\n", 2)[2],
            'filename': entry.meta['filename'],
            'lineno': entry.meta['lineno'],
            'journal': _journal(matching_entries),
        }

    def linechart_data(self, account_name):
        journal = self.account_journal(account_name)

        return [{
            'date': journal_entry['date'],
            'balance': journal_entry['balance'] if journal_entry['balance']
            # when there's no holding, the balance would be an empty dict.
            # Synthesize a zero balance based on the 'change' value
            else {x: 0 for x in journal_entry['change']}
        } for journal_entry in journal if 'balance' in journal_entry]

    def source_files(self):
        # Make sure the included source files are sorted, behind the main
        # source file
        return [self.beancount_file_path] + \
            sorted(filter(
                lambda x: x != self.beancount_file_path,
                [os.path.join(
                    os.path.dirname(self.beancount_file_path), filename)
                 for filename in self.options['include']]))

    def source(self, file_path):
        if file_path not in self.source_files():
            raise FavaAPIException('Trying to read a non-source file')

        with open(file_path, encoding='utf8') as file:
            source = file.read()
        return source

    def set_source(self, file_path, source):
        if file_path not in self.source_files():
            raise FavaAPIException('Trying to write a non-source file')

        with open(file_path, 'w+', encoding='utf8') as file:
            file.write(source)
        self.load_file()

    def commodity_pairs(self):
        fw_pairs = self.price_map.forward_pairs
        bw_pairs = []
        for a, b in fw_pairs:
            if (a in self.options['operating_currency'] and
                    b in self.options['operating_currency']):
                bw_pairs.append((b, a))
        return sorted(fw_pairs + bw_pairs)

    def prices(self, base, quote):
        all_prices = prices.get_all_prices(self.price_map,
                                           "{}/{}".format(base, quote))

        if self.filters['time']:
            return [(date, price) for date, price in all_prices
                    if (date >= self.filters['time'].begin_date and
                        date < self.filters['time'].end_date)]
        else:
            return all_prices

    def _activity_by_account(self, account_name=None):
        nb_activity_by_account = []
        for real_account in realization.iter_children(self.root_account):
            if not isinstance(real_account, RealAccount):
                continue
            if account_name and real_account.account != account_name:
                continue

            last_posting = realization.find_last_active_posting(
                real_account.txn_postings)

            if last_posting is None or isinstance(last_posting, Close):
                continue

            entry = get_entry(last_posting)

            nb_activity_by_account.append({
                'account': real_account.account,
                'last_posting_date': entry.date,
                'last_posting_filename': entry.meta['filename'],
                'last_posting_lineno': entry.meta['lineno']
            })

        return nb_activity_by_account

    def inventory(self, account_name):
        return compute_entries_balance(self.entries, prefix=account_name)

    def statistics(self, account_name=None):
        if account_name:
            activity_by_account = self._activity_by_account(account_name)
            if len(activity_by_account) == 1:
                return activity_by_account[0]
            else:
                return None

        grouped_entries = misc_utils.groupby(type, self.entries)
        entries_by_type = {type.__name__: len(entries)
                           for type, entries in grouped_entries.items()}

        all_postings = [posting
                        for entry in self.entries
                        if isinstance(entry, Transaction)
                        for posting in entry.postings]

        grouped_postings = misc_utils.groupby(
            lambda posting: posting.account, all_postings)
        postings_by_account = {account: len(postings)
                               for account, postings
                               in grouped_postings.items()}

        return {
            'entries_by_type': entries_by_type,
            'postings_by_account': postings_by_account,
            'activity_by_account': self._activity_by_account()
        }

    def is_valid_document(self, file_path):
        """Check if the given file_path is present in one of the
           Document entries or in a "statement"-metadata in a Transaction
           entry.

           :param file_path: A path to a file.
           :return: True when the file_path is refered to in a Document entry,
                    False otherwise.
        """
        is_present = False
        for entry in misc_utils.filter_type(self.entries, Document):
            if entry.filename == file_path:
                is_present = True

        if not is_present:
            for entry in misc_utils.filter_type(self.entries, Transaction):
                if 'statement' in entry.meta and \
                        entry.meta['statement'] == file_path:
                    is_present = True

        return is_present

    def query(self, query_string, numberify=False):
        return query.run_query(self.all_entries, self.options,
                               query_string, numberify=numberify)

    def _last_balance_or_transaction(self, account_name):
        real_account = realization.get_or_create(self.all_root_account,
                                                 account_name)

        for txn_posting in reversed(real_account.txn_postings):
            if not isinstance(txn_posting, (TxnPosting, Balance)):
                continue

            if isinstance(txn_posting, TxnPosting) and \
               txn_posting.txn.flag == flags.FLAG_UNREALIZED:
                continue
            return txn_posting

    def is_account_uptodate(self, account_name):
        """
        green:  if the last entry is a balance check that passed
        red:    if the last entry is a balance check that failed
        yellow: if the last entry is not a balance check
        """
        last_posting = self._last_balance_or_transaction(account_name)

        if last_posting:
            if isinstance(last_posting, Balance):
                if last_posting.diff_amount:
                    return 'red'
                else:
                    return 'green'
            else:
                return 'yellow'

    def last_account_activity_in_days(self, account_name):
        real_account = realization.get_or_create(self.all_root_account,
                                                 account_name)

        last_posting = realization.find_last_active_posting(
            real_account.txn_postings)

        if last_posting is None or isinstance(last_posting, Close):
            return 0

        entry = get_entry(last_posting)

        return (datetime.date.today() - entry.date).days

    def account_open_metadata(self, account_name):
        real_account = realization.get_or_create(self.root_account,
                                                 account_name)
        postings = realization.get_postings(real_account)
        for posting in postings:
            if isinstance(posting, Open):
                return posting.meta
        return {}
