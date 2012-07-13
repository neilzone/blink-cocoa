# Copyright (C) 2009-2011 AG Projects. See LICENSE for details.
#

from __future__ import with_statement

__all__ = ['BlinkContact', 'BlinkGroup', 'ContactListModel', 'contactIconPathForURI', 'loadContactIconFromFile', 'saveContactIconToFile']

import bisect
import base64
import datetime
import glob
import os
import re
import cPickle
import unicodedata

import AddressBook
from Foundation import *
from AppKit import *

from application.notification import NotificationCenter, IObserver
from application.python import Null
from application.system import makedirs, unlink
from sipsimple.configuration import DuplicateIDError
from sipsimple.configuration.settings import SIPSimpleSettings
from sipsimple.core import FrozenSIPURI, SIPURI
from sipsimple.addressbook import AddressbookManager, Contact, ContactURI, Group
from sipsimple.account import AccountManager, BonjourAccount
from sipsimple.threading.green import run_in_green_thread
from sipsimple.util import TimestampedNotificationData
from zope.interface import implements

from ContactController import AddContactController, EditContactController
from GroupController import AddGroupController
from AudioSession import AudioSession
from BlinkLogger import BlinkLogger
from HistoryManager import SessionHistory
from SIPManager import PresenceStatusList

from resources import ApplicationData
from util import *

ICON_SIZE=64


def base64Icon(icon):
    if not icon:
        return None

    originalSize = icon.size()
    if originalSize.width > ICON_SIZE or originalSize.height > ICON_SIZE:
        resizeWidth = ICON_SIZE
        resizeHeight = ICON_SIZE * originalSize.height/originalSize.width
        scaled_icon = NSImage.alloc().initWithSize_(NSMakeSize(resizeWidth, resizeHeight))
        scaled_icon.lockFocus()
        icon.drawInRect_fromRect_operation_fraction_(NSMakeRect(0, 0, resizeWidth, resizeHeight), NSMakeRect(0, 0, originalSize.width, originalSize.height), NSCompositeSourceOver, 1.0)
        scaled_icon.unlockFocus()
        tiff_data = scaled_icon.TIFFRepresentation()
    else:
        tiff_data = icon.TIFFRepresentation()

    bitmap_data = NSBitmapImageRep.alloc().initWithData_(tiff_data)
    png_data = bitmap_data.representationUsingType_properties_(NSPNGFileType, None)
    return base64.b64encode(png_data)

PresenceActivityPrefix = {
    "Available": "is",
    "Working": "is",
    "Appointment": "has an",
    "Busy": "is",
    "Breakfast": "is having",
    "Lunch": "is having",
    "Dinner": "is having",
    "Travel": "is in",
    "Driving": "is",
    "Playing": "is",
    "Spectator": "is a",
    "TV": "is watching",
    "Away": "is",
    "Invisible": "is",
    "Meeting": "is in a",
    "On the phone": "is",
    "Presentation": "is at a",
    "Performance": "gives a",
    "Sleeping": "is",
    "Vacation": "is in",
    "Holiday": "is in"
    }

def contactIconPathForURI(uri):
    return ApplicationData.get('photos/%s.tiff' % uri)


def saveContactIconToFile(image, uri):
    path = contactIconPathForURI(uri)
    makedirs(os.path.dirname(path))
    if image is not None:
        data = image.TIFFRepresentationUsingCompression_factor_(NSTIFFCompressionLZW, 1)
        data.writeToFile_atomically_(path, False)
    else:
        try:
            os.remove(path)
        except OSError:
            pass


def loadContactIconFromFile(uri):
    path = contactIconPathForURI(uri)
    if os.path.exists(path):
        return NSImage.alloc().initWithContentsOfFile_(path)
    return None


def loadContactIcon(contact):
    if contact.icon is not None:
        try:
            data = base64.b64decode(contact.icon)
            return NSImage.alloc().initWithData_(NSData.alloc().initWithBytes_length_(data, len(data)))
        except Exception:
            pass
    return None

def formatABPersonName(person):
    first = person.valueForProperty_(AddressBook.kABFirstNameProperty)
    last = person.valueForProperty_(AddressBook.kABLastNameProperty)
    middle = person.valueForProperty_(AddressBook.kABMiddleNameProperty)
    name = u""
    if first and last and middle:
        name += unicode(first) + " " + unicode(middle) + " " + unicode(last)
    elif first and last:
        name += unicode(first) + " " + unicode(last)
    elif last:
        name += unicode(last)
    elif first:
        name += unicode(first)
    return name


class BlinkContact(NSObject):
    """Basic Contact representation in Blink UI"""
    editable = True
    deletable = True
    auto_answer = False
    nc = NotificationCenter()

    def __new__(cls, *args, **kwargs):
        return cls.alloc().init()

    def __init__(self, uri, uri_type=None, name=None, icon=None):
        self.id = None
        self.contact = None
        self.uri = uri
        self.uris = [ContactURI(uri=self.uri, type=format_uri_type(uri_type))]
        self.name = NSString.stringWithString_(name or uri)
        self.detail = NSString.stringWithString_(uri)
        self.icon = icon
        self._preferred_media = 'audio'
        self._set_username_and_domain()

    @property
    def aliases(self):
        return list(alias.uri for alias in iter(self.uris) if alias.uri != self.uri)

    @property
    def model(self):
        return NSApp.delegate().contactsWindowController.model

    @property
    def favorite(self):
        return False

    def dealloc(self):
        self.icon = None
        self.nc = None
        super(BlinkContact, self).dealloc()

    def _set_username_and_domain(self):
        # save username and domain to speed up name lookups in the contacts list
        uri_string = self.uri
        if '@' in uri_string:
            self.username = uri_string.partition("@")[0]
            self.username = sip_prefix_pattern.sub("", self.username)
            domain = uri_string.partition("@")[-1]
            self.domain = domain if ':' not in domain else domain.partition(":")[0]
        else:
            self.username = uri_string.partition(":")[0] if ':' in uri_string else uri_string
            default_account = AccountManager().default_account
            self.domain = default_account.id.domain if default_account is not None and default_account is not BonjourAccount else ''

    def copyWithZone_(self, zone):
        return self

    def __str__(self):
        return "<%s: %s>" % (self.__class__.__name__, self.uri)

    __repr__ = __str__

    def __contains__(self, text):
        text = text.lower()
        return text in self.uri.lower() or text in self.name.lower()

    @property
    def preferred_media(self):
        _split = str(self.uri).split(';')
        for item in _split[:]:
            if not item.startswith("session-type"):
                _split.remove(item)
        try:
            session_type = _split[0].split("=")[1]
        except IndexError:
            session_type = None
        return session_type or self._preferred_media

    def split_uri(self, uri):
        if isinstance(uri, (FrozenSIPURI, SIPURI)):
            return (uri.user or '', uri.host or '')
        elif '@' in uri:
            uri = sip_prefix_pattern.sub("", uri)
            user, _, host = uri.partition("@")
            host = host.partition(":")[0]
            return (user, host)
        else:
            user = uri.partition(":")[0]
            return (user, '')

    def matchesURI(self, uri):

        def match(me, candidate):
            # check exact match
            if not len(candidate[1]):
                if me[0].startswith(candidate[0]):
                    return True
            else:
                if (me[0], me[1]) == (candidate[0], candidate[1]):
                    return True

            # check when a phone number, if the end matches
            # remove special characters used by Address Book contacts
            me_username=strip_addressbook_special_characters(me[0])

            # remove leading plus if present
            me_username = me_username.lstrip("+")

            # first strip leading + from the candidate
            candidate_username = candidate[0].lstrip("+")

            # then strip leading 0s from the candidate
            candidate_username = candidate_username.lstrip("0")

            # now check if they're both numbers
            if any(d not in "1234567890" for d in me_username + candidate_username) or not me_username or not candidate_username:
                return False

            # check if the trimmed candidate matches the end of the username if the number is long enough
            return len(candidate_username) > 7 and me_username.endswith(candidate_username)

        candidate = self.split_uri(uri)
        if match((self.username, self.domain), candidate):
            return True

        return any(match(self.split_uri(alias), candidate) for alias in self.aliases) if hasattr(self, "aliases") else False

    def setURI(self, uri):
        self.uri = uri

    def setURIs(self, uris):
        self.uris = self.contact.uris
        if self.contact.default_uri:
            self.uri = self.contact.default_uri
        else:
            try:
                first_uri = next(iter(self.contact.uris))
                self.uri = first_uri.uri
            except StopIteration:
                self.uri = ''
        self.detail = NSString.stringWithString_(self.uri)

    def setName(self, name):
        self.name = NSString.stringWithString_(name)

    def setDetail(self, detail):
        self.detail = NSString.stringWithString_(detail)

    def setPreferredMedia(self, media):
        self._preferred_media = media

    def setAutoAnswer(self, value):
        self.auto_answer = value

    def iconPath(self):
        return contactIconPathForURI(str(self.uri))

    def setIcon(self, image):
        if image:
            size = image.size()
            if size.width > ICON_SIZE or size.height > ICON_SIZE:
                image.setScalesWhenResized_(True)
                image.setSize_(NSMakeSize(ICON_SIZE, ICON_SIZE * size.height/size.width))

        self.icon = image
        self.saveIcon()

    def saveIcon(self):
        saveContactIconToFile(self.icon, str(self.uri))


class BlinkConferenceContact(BlinkContact):
    """Contact representation for conference drawer UI"""

    def __init__(self, *args, **kw):
        self.contact = None
    	BlinkContact.__init__(self, *args, **kw)
        self.active_media = []
        self.screensharing_url = None

    def setActiveMedia(self, media):
        self.active_media = media

    def setScreensharingUrl(self, url):
        self.screensharing_url = url


class BlinkPresenceContact(BlinkContact):
    """Contact representation with Presence Enabled"""

    def __init__(self, contact):
        self.id = contact.id
        self.contact = contact
        self.uris = self.contact.uris
        if self.contact.default_uri:
            self.uri = self.contact.default_uri
        else:
            try:
                first_uri = next(iter(self.contact.uris))
                self.uri = first_uri.uri
            except StopIteration:
                self.uri = ''

        self.name = NSString.stringWithString_(self.contact.name or self.uri)
        self.detail = NSString.stringWithString_(self.uri)
        self.icon = loadContactIcon(self.contact) or loadContactIconFromFile(self.uri)
        self._preferred_media = self.contact.preferred_media or 'audio'
        self._set_username_and_domain()

        # presence related attributes
        self.presence_indicator = 'unknown'
        self.presence_note = None
        self.presence_activity = None
        self.supported_media = []

        self.name = NSString.stringWithString_(self.contact.name or self.uri)
        self.detail = NSString.stringWithString_(self.uri)

    def _get_favorite(self):
        favorite_group = self.model.favorites_group.group
        return self.contact.id in favorite_group.contacts
    def _set_favorite(self, favorite):
        group = self.model.favorites_group.group
        operation = group.contacts.add if favorite else group.contacts.remove
        try:
            operation(self.contact)
        except ValueError:
            pass
        else:
            group.save()
    favorite = property(_get_favorite, _set_favorite)
    del _get_favorite, _set_favorite

    def setPresenceIndicator(self, indicator):
        self.presence_indicator = indicator

    def setPresenceNote(self, note):
        self.presence_note = note

    def setPresenceActivity(self, activity):
        self.presence_activity = activity

    def setSupportedMedia(self, media):
        self.supported_media = media

    def setIcon(self, icon=None):
        self.icon = icon

    def saveIcon(self):
        if self.icon:
            originalSize = self.icon.size()
            if originalSize.width > ICON_SIZE or originalSize.height > ICON_SIZE:
                resizeWidth = ICON_SIZE
                resizeHeight = ICON_SIZE * originalSize.height/originalSize.width
                scaled_icon = NSImage.alloc().initWithSize_(NSMakeSize(resizeWidth, resizeHeight))
                scaled_icon.lockFocus()
                self.icon.drawInRect_fromRect_operation_fraction_(NSMakeRect(0, 0, resizeWidth, resizeHeight), NSMakeRect(0, 0, originalSize.width, originalSize.height), NSCompositeSourceOver, 1.0)
                scaled_icon.unlockFocus()
                saveContactIconToFile(scaled_icon, str(self.uri))
                base64icon = base64Icon(scaled_icon)
            else:
                saveContactIconToFile(self.icon, str(self.uri))
                base64icon = base64Icon(self.icon)

            self.contact.icon = base64icon
            self.contact.save()
        else:
            saveContactIconToFile(None, str(self.uri))
            self.contact.icon = None
            self.contact.save()


class HistoryBlinkContact(BlinkContact):
    """Contact representation for history drawer"""
    editable = False
    deletable = False


class BonjourBlinkContact(BlinkContact):
    """Contact representation for a Bonjour contact"""
    editable = False
    deletable = False

    def __init__(self, uri, bonjour_neighbour, name=None, icon=None, detail=None):
        self.uri = str(uri)
        self.bonjour_neighbour = bonjour_neighbour
        self.aor = uri
        self.name = NSString.stringWithString_(name or self.uri)
        self.detail = NSString.stringWithString_(detail or self.uri)
        self.icon = NSImage.imageNamed_("NSUserGroup") if icon is None and ";isfocus" in self.uri else icon

        self.presence_indicator = None
        self.presence_note = None
        self.presence_activity = None
        self.supported_media = []

        self._preferred_media = 'audio'
        self._set_username_and_domain()

    def setPresenceIndicator(self, indicator):
        self.presence_indicator = indicator

    def setPresenceNote(self, note):
        self.presence_note = note

    def setPresenceActivity(self, activity):
        self.presence_activity = activity

    def setSupportedMedia(self, media):
        self.supported_media = media

    def matchesURI(self, uri):
        candidate = self.split_uri(uri)
        return (self.username, self.domain) == (candidate[0], candidate[1])

class SearchResultContact(BlinkContact):
    """Contact representation for un-matched results in the search outline"""
    editable = False
    deletable = False


class LdapSearchResultContact(BlinkContact):
    """Contact representation for LDAP results in the search outline"""
    editable = False
    deletable = False


class SystemAddressBookBlinkContact(BlinkContact):
    """Contact representation for system Address Book entries"""
    editable = True
    deletable = False

    def __init__(self, ab_contact):
        self.id = ab_contact.uniqueId()

        name = formatABPersonName(ab_contact)
        company = ab_contact.valueForProperty_(AddressBook.kABOrganizationProperty)

        if not name and company:
            name = unicode(company)

        self.name = name
        addresses = []

        labelNames = {
            AddressBook.kABPhoneWorkLabel:   "work",
            AddressBook.kABPhoneWorkFAXLabel: "fax",
            AddressBook.kABPhoneHomeFAXLabel: "fax",
            AddressBook.kABPhoneHomeLabel:   "home",
            AddressBook.kABPhoneMainLabel:   "main",
            AddressBook.kABPhoneMobileLabel: "mobile",
            AddressBook.kABOtherLabel:       "other"
            }

        # get phone numbers from the Phone section
        value = ab_contact.valueForProperty_(AddressBook.kABPhoneProperty)
        if value:
            for n in range(value.count()):
                label = value.labelAtIndex_(n)
                uri = unicode(value.valueAtIndex_(n))
                if labelNames.get(label, None) != 'fax':
                    addresses.append((labelNames.get(label, label), sip_prefix_pattern.sub("", uri)))

        # get SIP addresses from the Email section
        value = ab_contact.valueForProperty_(AddressBook.kABEmailProperty)
        if value:
            for n in range(value.count()):
                label = value.labelAtIndex_(n)
                uri = unicode(value.valueAtIndex_(n))
                if label == 'sip' or uri.startswith(("sip:", "sips:")):
                    addresses.append(('sip', sip_prefix_pattern.sub("", uri)))

        # get SIP addresses from the URLs section
        value = ab_contact.valueForProperty_(AddressBook.kABURLsProperty)
        if value:
            for n in range(value.count()):
                label = value.labelAtIndex_(n)
                uri = unicode(value.valueAtIndex_(n))
                if label == 'sip' or uri.startswith(("sip:", "sips:")):
                    addresses.append(('sip', sip_prefix_pattern.sub("", uri)))

        uris = []
        for address_type, address in addresses:
            if not address:
                continue

            # strip everything that's not numbers from the URIs if they are not SIP URIs
            if "@" not in address:
                if address.startswith("sip:"):
                    address = address[4:]
                contact_uri = "+" if address[0] == "+" else ""
                contact_uri += "".join(c for c in address if c in "0123456789#*")
            else:
                contact_uri = address
            uris.append(ContactURI(uri=contact_uri, type=address_type))

        self.uris = uris
        if self.uris:
            first_uri = next(iter(self.uris))
            self.uri = first_uri.uri
            self.detail = NSString.stringWithString_('%s (%s)' % (first_uri.uri, first_uri.type) )
        else:
            self.uri = 'None'
            self.detail = NSString.stringWithString_('None')
        self.default_uri = self.uri
        self.name = NSString.stringWithString_(self.name or self.uri)
        idata = ab_contact.imageData()
        self.icon = NSImage.alloc().initWithData_(idata) if idata else NSImage.imageNamed_("NSUser")
        self._preferred_media = 'audio'
        self._set_username_and_domain()


class BlinkGroup(NSObject):
    """Basic Group representation in Blink UI"""
    implements(IObserver)
    nc = NotificationCenter()

    type = None
    deletable = True
    ignore_search = True
    add_contact_allowed = True
    remove_contact_allowed = True
    delete_contact_allowed = True

    def __new__(cls, *args, **kwargs):
        return cls.alloc().init()

    def __init__(self, name=None, group=None):
        self.name = NSString.stringWithString_(name)
        self.group = group
        self.contacts = []

    def copyWithZone_(self, zone):
        return self

    @allocate_autorelease_pool
    @run_in_gui_thread
    def handle_notification(self, notification):
        handler = getattr(self, '_NH_%s' % notification.name, Null)
        handler(notification)

    def sortContacts(self):
        self.contacts.sort(key=lambda item: unicode(getattr(item, 'name')).lower())

    def setReference(self):
        if self.type:
            addressbook_manager = AddressbookManager()
            try:
                group = addressbook_manager.get_group(self.type)
            except KeyError:
                group = Group(id=self.type)
                group.name = self.name
                group.type  = self.type
                group.expanded = True
                group.position = None
                group.save()
            self.group = group


class BonjourBlinkGroup(BlinkGroup):
    """Group representation for Bonjour Neigborhood"""
    type = 'bonjour'
    deletable = False
    ignore_search = False

    add_contact_allowed = False
    remove_contact_allowed = False
    delete_contact_allowed = False

    def __init__(self, name=u'Bonjour Neighbours'):
        super(BonjourBlinkGroup, self).__init__(name)
        self.not_filtered_contacts = [] # keep a list of all neighbors so that we can rebuild the contacts when the sip transport changes, by default TLS transport is preferred


class NoBlinkGroup(BlinkGroup):
    type = 'no_group'
    deletable = False
    ignore_search = True

    add_contact_allowed = False
    remove_contact_allowed = False
    delete_contact_allowed = True

    def __init__(self, name=u'No Group'):
        super(NoBlinkGroup, self).__init__(name)


class AllContactsBlinkGroup(BlinkGroup):
    """Group representation for all contacts"""
    type = 'all_contacts'
    deletable = False
    ignore_search = False

    add_contact_allowed = False
    remove_contact_allowed = False
    delete_contact_allowed = True

    def __init__(self, name=u'All Contacts'):
        super(AllContactsBlinkGroup, self).__init__(name)

    def setReference(self):
        addressbook_manager = AddressbookManager()
        try:
            group = addressbook_manager.get_group(self.type)
        except KeyError:
            group = Group(id=self.type)
            group.name = self.name
            group.type  = self.type
            group.expanded = False
            group.position = None
            group.save()
        self.group = group


class FavoritesBlinkGroup(BlinkGroup):
    """Group representation for Favorites"""
    type = 'favorites'
    deletable = True
    ignore_search = True

    add_contact_allowed = True
    remove_contact_allowed = True
    delete_contact_allowed = True

    def __init__(self, name=u'Favorites'):
        super(FavoritesBlinkGroup, self).__init__(name)


class HistoryBlinkGroup(BlinkGroup):
    """Group representation for missed, incoming and outgoing calls dynamic groups"""
    type = 'history'
    deletable = False
    ignore_search = True

    add_contact_allowed = False
    remove_contact_allowed = False
    delete_contact_allowed = False

    def startTimer(self):
        # contacts are not yet loaded when building this group so we cannot lookup contacts just yet
        self.timer = NSTimer.timerWithTimeInterval_target_selector_userInfo_repeats_(6.0, self, "firstLoadTimer:", None, False)
        NSRunLoop.currentRunLoop().addTimer_forMode_(self.timer, NSRunLoopCommonModes)
        NSRunLoop.currentRunLoop().addTimer_forMode_(self.timer, NSEventTrackingRunLoopMode)

    def firstLoadTimer_(self, timer):
        self.load_history()
        if self.timer and self.timer.isValid():
            self.timer.invalidate()
            self.timer = None

    def format_date(self, dt):
        if not dt:
            return "unknown"
        now = datetime.datetime.now()
        delta = now - dt
        if (dt.year,dt.month,dt.day) == (now.year,now.month,now.day):
            return dt.strftime("at %H:%M")
        elif delta.days <= 1:
            return "Yesterday at %s" % dt.strftime("%H:%M")
        elif delta.days < 7:
            return dt.strftime("on %A")
        elif delta.days < 300:
            return dt.strftime("on %B %d")
        else:
            return dt.strftime("on %Y-%m-%d")

    @run_in_green_thread
    def load_history(self):
        results = self.get_history_entries()
        self.refresh_contacts(results)

    @allocate_autorelease_pool
    @run_in_gui_thread
    def refresh_contacts(self, results):
        self.contacts = []
        seen = {}
        contacts = []
        settings = SIPSimpleSettings()
        count = settings.contacts.maximum_calls
        for result in results:
            target_uri, name, full_uri, fancy_uri = sipuri_components_from_string(result.remote_uri)

            if seen.has_key(target_uri):
                seen[target_uri] += 1
            else:
                seen[target_uri] = 1
                getContactMatchingURI = NSApp.delegate().contactsWindowController.getContactMatchingURI
                contact = getContactMatchingURI(target_uri)
                if contact:
                    name = contact.name
                    icon=contact.icon
                else:
                    icon=None
                blink_contact = HistoryBlinkContact(target_uri, icon=icon , name=name)
                blink_contact.setDetail(u'%s call %s' % (self.type.capitalize(), self.format_date(result.start_time)))
                contacts.append(blink_contact)

            if len(seen) >= count:
                break

        for blink_contact in contacts:
            if seen[blink_contact.uri] > 1:
                new_detail = blink_contact.detail + u' and %d other times' % seen[blink_contact.uri]
                blink_contact.setDetail(new_detail)
            self.contacts.append(blink_contact)

        self.nc.post_notification("BlinkContactsHaveChanged", sender=self, data=TimestampedNotificationData())


class MissedCallsBlinkGroup(HistoryBlinkGroup):
    type = 'missed'

    def __init__(self, name=u'Missed Calls'):
        super(MissedCallsBlinkGroup, self).__init__(name)
        self.startTimer()

    def get_history_entries(self):
        return SessionHistory().get_entries(direction='incoming', status='missed', count=100, remote_focus="0")


class OutgoingCallsBlinkGroup(HistoryBlinkGroup):
    type = 'outgoing'

    def __init__(self, name=u'Outgoing Calls'):
        super(OutgoingCallsBlinkGroup, self).__init__(name)
        self.startTimer()

    def get_history_entries(self):
        return SessionHistory().get_entries(direction='outgoing', count=100, remote_focus="0")


class IncomingCallsBlinkGroup(HistoryBlinkGroup):
    type = 'incoming'

    def __init__(self, name=u'Incoming Calls'):
        super(IncomingCallsBlinkGroup, self).__init__(name)
        self.startTimer()

    def get_history_entries(self):
        return SessionHistory().get_entries(direction='incoming', status='completed', count=100, remote_focus="0")


class AddressBookBlinkGroup(BlinkGroup):
    """Address Book Group representation in Blink UI"""
    type = 'addressbook'
    deletable = False
    ignore_search = False

    add_contact_allowed = False
    remove_contact_allowed = False
    delete_contact_allowed = False

    def __init__(self, name=u'Address Book'):
        super(AddressBookBlinkGroup, self).__init__(name)

    def loadAddressBook(self):
        self.contacts = []

        book = AddressBook.ABAddressBook.sharedAddressBook()
        if book is None:
            return

        for ab_contact in book.people():
            blink_contact = SystemAddressBookBlinkContact(ab_contact)
            if blink_contact.uri and blink_contact.uri != 'None':
                self.contacts.append(blink_contact)

        self.sortContacts()

    def setReference(self):
        addressbook_manager = AddressbookManager()
        try:
            group = addressbook_manager.get_group(self.type)
        except KeyError:
            group = Group(id=self.type)
            group.name = self.name
            group.type  = self.type
            group.expanded = False
            group.position = None
            group.save()
        self.group = group


class CustomListModel(NSObject):
    """Contacts List Model behaviour, display and drag an drop actions"""
    groupsList = []
    drop_on_contact_index = None

    @property
    def sessionControllersManager(self):
        return NSApp.delegate().contactsWindowController.sessionControllersManager

    # data source methods
    def outlineView_numberOfChildrenOfItem_(self, outline, item):
        if item is None:
            return len(self.groupsList)
        elif isinstance(item, BlinkGroup):
            return len(item.contacts)
        else:
            return 0

    def outlineView_shouldEditTableColumn_item_(self, outline, column, item):
        return isinstance(item, BlinkGroup)

    def outlineView_isItemExpandable_(self, outline, item):
        return item is None or isinstance(item, BlinkGroup)

    def outlineView_objectValueForTableColumn_byItem_(self, outline, column, item):
        return item and item.name

    def outlineView_setObjectValue_forTableColumn_byItem_(self, outline, object, column, item):
        if isinstance(item, BlinkGroup) and object != item.name:
            item.group.name = object
            item.group.save()

    def outlineView_itemForPersistentObject_(self, outline, object):
        try:
            return (group for group in self.groupsList if group.name == object).next()
        except StopIteration:
            return None

    def outlineView_persistentObjectForItem_(self, outline, item):
        return item and item.name

    def outlineView_child_ofItem_(self, outline, index, item):
        if item is None:
            return self.groupsList[index]
        elif isinstance(item, BlinkGroup):
            try:
                return item.contacts[index]
            except IndexError:
                pass
        return None

    def outlineView_heightOfRowByItem_(self, outline, item):
        return 18 if isinstance(item, BlinkGroup) else 34

    # delegate methods
    def outlineView_isGroupItem_(self, outline, item):
        return isinstance(item, BlinkGroup)

    def outlineView_willDisplayCell_forTableColumn_item_(self, outline, cell, column, item):
        cell.setMessageIcon_(None)

        if isinstance(item, BlinkContact):
            cell.setContact_(item)
        else:
            cell.setContact_(None)

    def outlineView_toolTipForCell_rect_tableColumn_item_mouseLocation_(self, ov, cell, rect, tc, item, mouse):
        if isinstance(item, BlinkContact):
            return (item.uri, rect)
        else:
            return (None, rect)

    # drag and drop
    def outlineView_validateDrop_proposedItem_proposedChildIndex_(self, table, info, proposed_item, index):
        self.drop_on_contact_index = None
        if info.draggingPasteboard().availableTypeFromArray_([NSFilenamesPboardType]):
            if index != NSOutlineViewDropOnItemIndex or not isinstance(proposed_item, (BlinkPresenceContact, BonjourBlinkContact)):
                return NSDragOperationNone

            fnames = info.draggingPasteboard().propertyListForType_(NSFilenamesPboardType)
            if not all(os.path.isfile(f) for f in fnames):
                return NSDragOperationNone
            return NSDragOperationCopy
        elif info.draggingPasteboard().availableTypeFromArray_(["x-blink-audio-session"]):
            source = info.draggingSource()
            if source.delegate is None or not source.delegate.canTransfer or not source.delegate.transferEnabled:
                return NSDragOperationNone

            if source.delegate.sessionController.account is not BonjourAccount() and isinstance(proposed_item, BonjourBlinkContact):
                return NSDragOperationNone

            if index != NSOutlineViewDropOnItemIndex or not isinstance(proposed_item, BlinkContact):
                return NSDragOperationNone

            return NSDragOperationGeneric
        else:
            if info.draggingSource() != table:
                return NSDragOperationNone

            group, blink_contact = eval(info.draggingPasteboard().stringForType_("dragged-contact"))
            if blink_contact is None:
                if isinstance(proposed_item, BlinkContact):
                    proposed_item = table.parentForItem_(proposed_item)

                if proposed_item == self.groupsList[group]:
                    return NSDragOperationNone

                try:
                    i = self.groupsList.index(proposed_item)
                except ValueError:
                    i = len(self.groupsList)
                    if group == i-1:
                        return NSDragOperationNone

                table.setDropItem_dropChildIndex_(None, i)
            else:
                sourceGroup = self.groupsList[group]
                sourceContact = sourceGroup.contacts[blink_contact]

                if isinstance(sourceGroup, BonjourBlinkGroup):
                    return NSDragOperationNone

                if isinstance(proposed_item, BlinkGroup):
                    targetGroup = proposed_item

                    if not targetGroup:
                        return NSDragOperationNone

                    if sourceGroup == targetGroup:
                        return NSDragOperationNone

                    if isinstance(targetGroup, (AllContactsBlinkGroup, NoBlinkGroup)):
                        return NSDragOperationNone

                    if isinstance(targetGroup, FavoritesBlinkGroup):
                        if sourceContact.favorite:
                            return NSDragOperationNone
                        else:
                            return NSDragOperationCopy

                    if not targetGroup.add_contact_allowed:
                        return NSDragOperationNone

                    if self.isBlinkContactInBlinkGroups(sourceContact, targetGroup):
                        return NSDragOperationNone

                    c = len(proposed_item.contacts) if index == NSOutlineViewDropOnItemIndex else index
                    i = self.groupsList.index(proposed_item)
                    table.setDropItem_dropChildIndex_(self.groupsList[i], c)
                else:
                    targetGroup = table.parentForItem_(proposed_item)

                    if not targetGroup:
                        return NSDragOperationNone

                    if sourceGroup == targetGroup and not targetGroup.add_contact_allowed:
                        return NSDragOperationNone

                    if isinstance(targetGroup, (AllContactsBlinkGroup, NoBlinkGroup)):
                        return NSDragOperationNone

                    if isinstance(targetGroup, FavoritesBlinkGroup):
                        if sourceContact.favorite:
                            return NSDragOperationNone
                        else:
                            return NSDragOperationCopy

                    if not targetGroup.add_contact_allowed:
                        return NSDragOperationNone

                    if index == NSOutlineViewDropOnItemIndex:
                        index = targetGroup.contacts.index(proposed_item)
                        self.drop_on_contact_index = index

                    if sourceGroup == targetGroup:
                        return NSDragOperationCopy

                    table.setDropItem_dropChildIndex_(targetGroup, index)

            return NSDragOperationMove

    def outlineView_acceptDrop_item_childIndex_(self, table, info, item, index):
        if info.draggingPasteboard().availableTypeFromArray_([NSFilenamesPboardType]):
            if index != NSOutlineViewDropOnItemIndex or not isinstance(item, (BlinkPresenceContact, BonjourBlinkContact)):
                return False

            filenames =[unicodedata.normalize('NFC', file) for file in info.draggingPasteboard().propertyListForType_(NSFilenamesPboardType)]
            account = BonjourAccount() if isinstance(item, BonjourBlinkContact) else AccountManager().default_account
            if filenames and account and self.sessionControllersManager.isMediaTypeSupported('file-transfer'):
                self.sessionControllersManager.send_files_to_contact(account, item.uri, filenames)
                return True
            return False
        elif info.draggingPasteboard().availableTypeFromArray_(["x-blink-audio-session"]):
            source = info.draggingSource()
            if index != NSOutlineViewDropOnItemIndex or not isinstance(item, BlinkContact) or not isinstance(source, AudioSession):
                return False
            if source.delegate is None:
                return False
            if not source.delegate.canTransfer or not source.delegate.transferEnabled:
                return False
            if len(item.uris) == 1:
                source.delegate.transferSession(item.uri)
            else:
                row = table.rowForItem_(item)
                point = table.window().convertScreenToBase_(NSEvent.mouseLocation())
                event = NSEvent.mouseEventWithType_location_modifierFlags_timestamp_windowNumber_context_eventNumber_clickCount_pressure_(
                                                                                NSLeftMouseUp, point, 0, NSDate.timeIntervalSinceReferenceDate(), table.window().windowNumber(),
                                                                                table.window().graphicsContext(), 0, 1, 0)
                transfer_menu = NSMenu.alloc().init()
                titem = transfer_menu.addItemWithTitle_action_keyEquivalent_(u'Transfer Call To', "", "")
                titem.setEnabled_(False)
                for uri in item.uris:
                    titem = transfer_menu.addItemWithTitle_action_keyEquivalent_('%s (%s)' % (uri.uri, uri.type), "userClickedBlindTransferMenuItem:", "")
                    titem.setIndentationLevel_(1)
                    titem.setTarget_(self)
                    titem.setRepresentedObject_({'source': source, 'destination': uri.uri})

                NSMenu.popUpContextMenu_withEvent_forView_(transfer_menu, event, table)

            return True
        else:
            if info.draggingSource() != table:
                return False
            group, blink_contact = eval(info.draggingPasteboard().stringForType_("dragged-contact"))
            if blink_contact is None:
                g = self.groupsList[group]
                del self.groupsList[group]
                if group > index:
                    self.groupsList.insert(index, g)
                else:
                    self.groupsList.insert(index-1, g)
                table.reloadData()
                if table.selectedRow() >= 0:
                    table.selectRowIndexes_byExtendingSelection_(NSIndexSet.indexSetWithIndex_(table.rowForItem_(g)), False)
                self.saveGroupPosition()
                return True
            else:
                sourceGroup = self.groupsList[group]
                sourceContact = sourceGroup.contacts[blink_contact]
                if isinstance(item, BlinkGroup):
                    targetGroup = item

                    if isinstance(sourceContact, SystemAddressBookBlinkContact) or isinstance(sourceGroup, HistoryBlinkGroup):
                        try:
                            uri_type = (uri.type for uri in sourceContact.uris if uri.uri == sourceContact.uri).next()
                        except StopIteration:
                            uri_type = None
                        self.addContact(sourceContact.uri, name=sourceContact.name, type=uri_type)
                        return

                    addressbook_manager = AddressbookManager()
                    with addressbook_manager.transaction():
                        targetGroup.group.contacts.add(sourceContact.contact)
                        targetGroup.group.save()

                        if not isinstance(targetGroup, FavoritesBlinkGroup) and sourceGroup.remove_contact_allowed:
                            sourceGroup.group.contacts.remove(sourceContact.contact)
                            sourceGroup.group.save()

                    row = table.rowForItem_(sourceContact)
                    if row>=0:
                        table.scrollRowToVisible_(row)

                    if table.selectedRow() >= 0:
                        table.selectRowIndexes_byExtendingSelection_(NSIndexSet.indexSetWithIndex_(row if row>=0 else 0), False)
                else:
                    targetGroup = table.parentForItem_(item)
                    if sourceGroup == targetGroup:
                        targetContact = targetGroup.contacts[self.drop_on_contact_index]

                        if (sourceContact.name == targetContact.name):
                            message = "Would you like to consolidate the two contacts into %s?" % targetContact.name
                        else:
                            message = u"Would you like to merge %s and %s contacts into %s?"%(sourceContact.name, targetContact.name, targetContact.name)

                        ret = NSRunAlertPanel(u"Merge Contacts", message, u"Merge", u"Cancel", None)
                        if ret != NSAlertDefaultReturn:
                            return

                        target_changed = 0
                        for new_uri in sourceContact.contact.uris:
                            try:
                                uri = (uri for uri in targetContact.contact.uris if uri.uri == new_uri.uri).next()
                            except StopIteration:
                                targetContact.contact.uris.add(new_uri)
                                target_changed += 1
                            if targetContact.contact.icon is None and sourceContact.contact.icon is not None:
                                targetContact.contact.icon = sourceContact.contact.icon
                                target_changed += 1

                        if target_changed:
                            targetContact.contact.save()
                        sourceContact.contact.delete()
                        return

                    if isinstance(sourceContact, SystemAddressBookBlinkContact):
                        try:
                            uri_type = (uri.type for uri in sourceContact.uris if uri.uri == sourceContact.uri).next()
                        except StopIteration:
                            uri_type = None
                        self.addContact(sourceContact.uri, name=sourceContact.name, type=uri_type)
                        return

                    targetGroup.group.contacts.add(sourceContact.contact)
                    targetGroup.group.save()
                    return True

                return True
            return False

    def outlineView_writeItems_toPasteboard_(self, table, items, pboard):
        if isinstance(items[0], BlinkGroup):
            try:
                group = self.groupsList.index(items[0])
            except ValueError:
                return False
            else:
                pboard.declareTypes_owner_(NSArray.arrayWithObject_("dragged-contact"), self)
                pboard.setString_forType_(str((group, None)), "dragged-contact")
                return True
        else:
            sourceGroup = table.parentForItem_(items[0])
            if isinstance(sourceGroup, BlinkGroup):
                contact_index = sourceGroup.contacts.index(items[0])
                group_index = self.groupsList.index(sourceGroup)
                pboard.declareTypes_owner_(["dragged-contact", "x-blink-sip-uri"], self)
                pboard.setString_forType_(str((group_index, contact_index)), "dragged-contact")
                pboard.setString_forType_(items[0].uri, "x-blink-sip-uri")
                return True
            elif isinstance(sourceGroup, SearchResultContact):
                pboard.declareTypes_owner_(["x-blink-sip-uri"], self)
                pboard.setString_forType_(items[0].uri, "x-blink-sip-uri")
                return True
            else:
                pboard.declareTypes_owner_(["x-blink-sip-uri"], self)
                pboard.setString_forType_(items[0].uri, "x-blink-sip-uri")
                return True

    def userClickedBlindTransferMenuItem_(self, sender):
        source = sender.representedObject()['source']
        destination = sender.representedObject()['destination']
        source.delegate.transferSession(destination)


class SearchContactListModel(CustomListModel):
    def init(self):
        return self


class ContactListModel(CustomListModel):
    """Blink Contacts List Model main implementation"""
    implements(IObserver)
    contactOutline = objc.IBOutlet()
    nc = NotificationCenter()

    def init(self):
        self.all_contacts_group = AllContactsBlinkGroup()
        self.no_group = NoBlinkGroup()
        self.bonjour_group = BonjourBlinkGroup()
        self.addressbook_group = AddressBookBlinkGroup()
        self.missed_calls_group = MissedCallsBlinkGroup()
        self.outgoing_calls_group = OutgoingCallsBlinkGroup()
        self.incoming_calls_group = IncomingCallsBlinkGroup()
        self.favorites_group = FavoritesBlinkGroup()
        self.contact_backup_timer = None

        return self

    @allocate_autorelease_pool
    @run_in_gui_thread
    def handle_notification(self, notification):
        handler = getattr(self, '_NH_%s' % notification.name, Null)
        handler(notification)

    def awakeFromNib(self):
        self.nc.add_observer(self, name="BonjourAccountDidAddNeighbour")
        self.nc.add_observer(self, name="BonjourAccountDidUpdateNeighbour")
        self.nc.add_observer(self, name="BonjourAccountDidRemoveNeighbour")
        self.nc.add_observer(self, name="CFGSettingsObjectDidChange")
        self.nc.add_observer(self, name="AddressbookContactWasActivated")
        self.nc.add_observer(self, name="AddressbookContactWasDeleted")
        self.nc.add_observer(self, name="AddressbookContactDidChange")
        self.nc.add_observer(self, name="AddressbookGroupWasCreated")
        self.nc.add_observer(self, name="AddressbookGroupWasActivated")
        self.nc.add_observer(self, name="AddressbookGroupWasDeleted")
        self.nc.add_observer(self, name="AddressbookGroupDidChange")
        self.nc.add_observer(self, name="SIPAccountDidActivate")
        self.nc.add_observer(self, name="SIPAccountDidDeactivate")
        self.nc.add_observer(self, name="SIPApplicationDidStart")
        self.nc.add_observer(self, name="SIPApplicationWillEnd")
        self.nc.add_observer(self, name="AudioCallLoggedToHistory")

        ns_nc = NSNotificationCenter.defaultCenter()
        ns_nc.addObserver_selector_name_object_(self, "groupExpanded:", NSOutlineViewItemDidExpandNotification, self.contactOutline)
        ns_nc.addObserver_selector_name_object_(self, "groupCollapsed:", NSOutlineViewItemDidCollapseNotification, self.contactOutline)
        ns_nc.addObserver_selector_name_object_(self, "reloadAddressbook:", AddressBook.kABDatabaseChangedNotification, None)
        ns_nc.addObserver_selector_name_object_(self, "reloadAddressbook:", AddressBook.kABDatabaseChangedExternallyNotification, None)

    def groupCollapsed_(self, notification):
        group = notification.userInfo()["NSObject"]
        if group.group:
            group.group.expanded = False
            group.group.save()

    def groupExpanded_(self, notification):
        group = notification.userInfo()["NSObject"]
        if group.group:
            group.group.expanded = True
            group.group.save()

    def reloadAddressbook_(self, notification):
        #try:
        #    inserted = notification.userInfo()[AddressBook.kABInsertedRecords]
        #except KeyError:
        #    inserted = None
        #try:
        #    deleted = notification.userInfo()[AddressBook.kABDeletedRecords]
        #except KeyError:
        #    deleted = None
        #try:
        #    updated = notification.userInfo()[AddressBook.kABUpdatedRecords]
        #except KeyError:
        #    updated = None
        #book = AddressBook.ABAddressBook.sharedAddressBook()
        settings = SIPSimpleSettings()
        if settings.contacts.enable_address_book:
            #for entries in (inserted, deleted, updated):
            #    if entries:
            #        for id in entries:
            #            match = book.recordForUniqueId_(id)
            #            if type(match) == AddressBook.ABPerson:
            #                name = formatABPersonName(match)
            #                #BlinkLogger().log_info(u"Address Book entry for %s has changed" % name)

            self.addressbook_group.loadAddressBook()
            self.nc.post_notification("BlinkContactsHaveChanged", sender=self, data=TimestampedNotificationData())

    def hasContactMatchingURI(self, uri):
        return any(blink_contact.matchesURI(uri) for group in self.groupsList if not group.ignore_search for blink_contact in group.contacts)

    def getContactMatchingURI(self, uri):
        try:
            return (blink_contact for group in self.groupsList if not group.ignore_search for blink_contact in group.contacts if blink_contact.matchesURI(uri)).next()
        except StopIteration:
            return None

    def checkContactBackup_(self, timer):
        now = datetime.datetime.now()
        for file in glob.glob('%s/*.pickle' % ApplicationData.get('contacts_backup')):
            backup_date, _ = os.path.splitext(os.path.basename(file))
            diff = now - datetime.datetime.strptime(backup_date, "%Y%m%d-%H%M%S")
            if diff.days <= 7:
                break
            elif diff.days > 120:
                unlink(file)
        else:
            self.backup_contacts(silent=True)

    def backup_contacts(self, silent=False):
        backup_contacts = []
        backup_groups = []

        for contact in AddressbookManager().get_contacts():
            backup_contact={
                'id'              : contact.id,
                'name'            : contact.name,
                'default_uri'     : contact.default_uri,
                'uris'            : list((alias.uri, alias.type) for alias in iter(contact.uris)),
                'preferred_media' : contact.preferred_media,
                'icon'            : contact.icon,
                'favorite'        : contact.favorite,
                'presence'        : {'policy': contact.presence.policy, 'subscribe': contact.presence.subscribe},
                'dialog'          : {'policy': contact.dialog.policy,   'subscribe': contact.dialog.subscribe}
            }
            backup_contacts.append(backup_contact)

        for group in AddressbookManager().get_groups():
            if group.type is not None:
                continue
            contacts = list(contact.id for contact in group.contacts)
            backup_group = {
                'id'      : group.id,
                'name'    : group.name,
                'contacts': contacts
            }
            backup_groups.append(backup_group)

        backup_data = {"contacts":backup_contacts, "groups": backup_groups, "version": 2}
        filename = "contacts_backup/%s.pickle" % (datetime.datetime.now().strftime("%Y%m%d-%H%M%S"))
        storage_path = ApplicationData.get(filename)
        makedirs(os.path.dirname(storage_path))

        if backup_contacts or backup_groups:
            try:
                cPickle.dump(backup_data, open(storage_path, "w+"))
                if not silent:
                    NSRunAlertPanel("Contacts Backup Successful", "%d contacts have been saved. You can restore them at a later time from Contacts/Restore menu." % len(backup_contacts), "OK", None, None)
            except (IOError, cPickle.PicklingError):
                pass
        else:
            if not silent:
                NSRunAlertPanel("Contacts Backup Unnecessary", "There are no contacts available for backup.", "OK", None, None)

    def restore_contacts(self, backup):
        restored_contacts = 0
        restored_groups = 0
        filename = backup[0]

        try:
            with open(filename, 'r') as f:
                data = cPickle.load(f)
        except (IOError, cPickle.UnpicklingError):
            return

        try:
            version = data['version']
        except KeyError:
            version = 1
            contacts_for_group = {}

        try:
            contacts = data['contacts']
        except (TypeError, KeyError):
            return

        if contacts:
            ret = NSRunAlertPanel(u"Restore Contacts", u"This operation will restore %d contacts present in the backup taken at %s. Newer contacts will be preserved. "%(len(data['contacts']), backup[1]), u"Restore", u"Cancel", None)
            if ret != NSAlertDefaultReturn:
                return
            seen_uri = {}
            addressbook_manager = AddressbookManager()
            with addressbook_manager.transaction():
                for backup_contact in contacts:
                    if version == 1:
                        try:
                            if backup_contact['uri'] in seen_uri.keys():
                                continue
                            if self.hasContactMatchingURI(backup_contact['uri']):
                                continue
                            contact = Contact()
                            contact.default_uri = backup_contact['uri']
                            contact.uris.add(ContactURI(uri=backup_contact['uri'], type='SIP'))
                            contact.name = backup_contact['name'] or contact.default_uri
                            contact.preferred_media = backup_contact['preferred_media'] or 'audio'
                            contact.icon = backup_contact['icon']
                            contact.favorite = backup_contact['favorite']
                            contact.save()
                            group = backup_contact['group']
                            if group:
                                try:
                                    contacts = contacts_for_group[group]
                                except KeyError:
                                    contacts_for_group[group] = [contact]
                                else:
                                    contacts_for_group[group].append(contact)
                            restored_contacts += 1
                            seen_uri[backup_contact['uri']] = True
                        except DuplicateIDError:
                            pass
                        except Exception, e:
                            BlinkLogger().log_info(u"Contacts restore failed: %s" % e)
                    elif version == 2:
                        try:
                            contact = Contact(id=backup_contact['id'])
                            contact.name = backup_contact['name']
                            contact.default_uri = backup_contact['default_uri']
                            for uri in backup_contact['uris']:
                                contact.uris.add(ContactURI(uri=uri[0], type=uri[1]))
                            contact.preferred_media = backup_contact['preferred_media']
                            contact.icon = backup_contact['icon']
                            contact.favorite = backup_contact['favorite']
                            presence = backup_contact['presence']
                            dialog = backup_contact['dialog']
                            contact.presence.policy = presence['policy']
                            contact.presence.subscribe = presence['subscribe']
                            contact.dialog.policy = dialog['policy']
                            contact.dialog.subscribe = dialog['subscribe']
                            contact.save()
                            restored_contacts += 1
                        except DuplicateIDError:
                            pass
                        except Exception, e:
                            BlinkLogger().log_info(u"Contacts restore failed: %s" % e)
                if version == 1:
                    for key in contacts_for_group.keys():
                        try:
                            group = (group for group in addressbook_manager.get_groups() if group.name == key).next()
                        except StopIteration:
                            group = Group()
                            group.name = key
                            restored_groups += 1
                            group.contacts = contacts_for_group[key]
                        else:
                            for c in contacts_for_group[key]:
                                group.contacts.add(c)
                        group.save()
                elif version == 2:
                    for backup_group in data['groups']:
                        try:
                            group = Group(id=backup_group['id'])
                            group.name = backup_group['name']
                            restored_groups += 1
                        except DuplicateIDError:
                            group = addressbook_manager.get_group(backup_group['id'])
                        for id in backup_group['contacts']:
                            try:
                                contact = addressbook_manager.get_contact(id)
                            except Exception:
                                pass
                            else:
                                group.contacts.add(contact)
                        group.save()

        panel_text = ''
        if not restored_contacts:
            panel_text += u"All contacts from the backup were already present and none has been restored. "
        elif restored_contacts == 1:
            panel_text += u"One contact has been restored. "
        else:
            panel_text += u"%d contacts have been restored. " % restored_contacts

        if not restored_groups:
            panel_text += u"All groups from the backup were already present and none has been restored. "
        elif restored_groups == 1:
            panel_text += u"One group has been restored. "
        else:
            panel_text += u"%d groups have been restored. " % restored_groups

        NSRunAlertPanel(u"Restore Completed", panel_text , u"OK", None, None)

    def _NH_SIPApplicationDidStart(self, notification):
        addressbook_manager = AddressbookManager()
        with addressbook_manager.transaction():
            self.all_contacts_group.setReference()
            self.no_group.setReference()
            self.addressbook_group.setReference()
            self.missed_calls_group.setReference()
            self.outgoing_calls_group.setReference()
            self.incoming_calls_group.setReference()
            self.favorites_group.setReference()

            self.nc.post_notification("BlinkContactsHaveChanged", sender=self, data=TimestampedNotificationData())

            if NSApp.delegate().contactsWindowController.first_run:
                self.createInitialGroupAndContacts()

            self._migrateContacts()

        self.contact_backup_timer = NSTimer.scheduledTimerWithTimeInterval_target_selector_userInfo_repeats_(3600.0, self, "checkContactBackup:", None, True)
        NSTimer.scheduledTimerWithTimeInterval_target_selector_userInfo_repeats_(30.0, self, "checkContactBackup:", None, False)

    def _NH_SIPApplicationWillEnd(self, notification):
        if self.contact_backup_timer is not None and self.contact_backup_timer.isValid():
            self.contact_backup_timer.invalidate()
        self.contact_backup_timer = None

    def _NH_AudioCallLoggedToHistory(self, notification):
        if NSApp.delegate().applicationName != 'Blink Lite':
            settings = SIPSimpleSettings()

            if settings.contacts.enable_missed_calls_group:
                self.missed_calls_group.load_history()

            if settings.contacts.enable_outgoing_calls_group:
                self.outgoing_calls_group.load_history()

            if settings.contacts.enable_incoming_calls_group:
                self.incoming_calls_group.load_history()

    def _NH_CFGSettingsObjectDidChange(self, notification):
        settings = SIPSimpleSettings()
        if notification.data.modified.has_key("contacts.enable_address_book"):
            if settings.contacts.enable_address_book and self.addressbook_group not in self.groupsList:
                self.addressbook_group.loadAddressBook()
                position = len(self.groupsList) if self.groupsList else 0
                self.groupsList.insert(position, self.addressbook_group)
                self.saveGroupPosition()
                self.nc.post_notification("BlinkContactsHaveChanged", sender=self, data=TimestampedNotificationData())
            elif not settings.contacts.enable_address_book and self.addressbook_group in self.groupsList:
                self.groupsList.remove(self.addressbook_group)
                self.saveGroupPosition()
                self.nc.post_notification("BlinkContactsHaveChanged", sender=self, data=TimestampedNotificationData())

        if notification.data.modified.has_key("contacts.enable_incoming_calls_group"):
            if settings.contacts.enable_incoming_calls_group and self.incoming_calls_group not in self.groupsList:
                self.incoming_calls_group.load_history()
                position = len(self.groupsList) if self.groupsList else 0
                self.groupsList.insert(position, self.incoming_calls_group)
                self.saveGroupPosition()
            elif not settings.contacts.enable_incoming_calls_group and self.incoming_calls_group in self.groupsList:
                self.groupsList.remove(self.incoming_calls_group)
                self.saveGroupPosition()
                self.nc.post_notification("BlinkContactsHaveChanged", sender=self, data=TimestampedNotificationData())

        if notification.data.modified.has_key("contacts.enable_outgoing_calls_group"):
            if settings.contacts.enable_outgoing_calls_group and self.outgoing_calls_group not in self.groupsList:
                self.outgoing_calls_group.load_history()
                position = len(self.groupsList) if self.groupsList else 0
                self.groupsList.insert(position, self.outgoing_calls_group)
                self.saveGroupPosition()
            elif not settings.contacts.enable_outgoing_calls_group and self.outgoing_calls_group in self.groupsList:
                self.groupsList.remove(self.outgoing_calls_group)
                self.saveGroupPosition()
                self.nc.post_notification("BlinkContactsHaveChanged", sender=self, data=TimestampedNotificationData())

        if notification.data.modified.has_key("contacts.enable_missed_calls_group"):
            if settings.contacts.enable_missed_calls_group and self.missed_calls_group not in self.groupsList:
                self.missed_calls_group.load_history()
                position = len(self.groupsList) if self.groupsList else 0
                self.groupsList.insert(position, self.missed_calls_group)
                self.saveGroupPosition()
            elif not settings.contacts.enable_missed_calls_group and self.missed_calls_group in self.groupsList:
                self.groupsList.remove(self.missed_calls_group)
                self.saveGroupPosition()
                self.nc.post_notification("BlinkContactsHaveChanged", sender=self, data=TimestampedNotificationData())

        if notification.data.modified.has_key("contacts.enable_favorites_group"):
            if settings.contacts.enable_favorites_group and self.favorites_group not in self.groupsList:
                self.groupsList.insert(0, self.favorites_group)
                self.saveGroupPosition()
                self.nc.post_notification("BlinkContactsHaveChanged", sender=self, data=TimestampedNotificationData())
            elif not settings.contacts.enable_favorites_group and self.favorites_group in self.groupsList:
                self.groupsList.remove(self.favorites_group)
                self.saveGroupPosition()
                self.nc.post_notification("BlinkContactsHaveChanged", sender=self, data=TimestampedNotificationData())

        if notification.data.modified.has_key("contacts.maximum_calls"):
            if settings.contacts.enable_missed_calls_group:
                self.missed_calls_group.load_history()

            if settings.contacts.enable_outgoing_calls_group:
                self.outgoing_calls_group.load_history()

            if settings.contacts.enable_incoming_calls_group:
                self.incoming_calls_group.load_history()

        if notification.data.modified.has_key("presence.enabled"):
            self.updatePresenceIndicator()

    def _migrateContacts(self):
        """Used in version 1.2.0 when switched over to new contacts model in sip simple sdk 0.18.3"""
        path = ApplicationData.get('contacts_')
        if not os.path.exists(path):
            return

        BlinkLogger().log_info(u"Migrating old contacts to the new model...")

        try:
            with open(path, 'r') as f:
                data = cPickle.load(f)
        except (IOError, cPickle.UnpicklingError):
            BlinkLogger().log_info(u"Couldn't load old contacts")
            return

        for group_item in data:
            if type(group_item) == tuple:
                if len(group_item) == 3:
                    group_item = (group_item[0], group_item[-1])
                group_item = {"name":group_item[0], "contacts":group_item[1], "expanded":True, "special": None}

            # workaround because the special attribute wasn't saved
            if "special" not in group_item:
                group_item["special"] = None

            if group_item["special"] is None:
                try:
                    group = Group()
                    group.name = group_item["name"]
                    group.expanded = group_item["expanded"]
                    group.type = group_item["special"]
                    group.position = None
                    group.save()
                except DuplicateIDError:
                    pass

                if group:
                    for pickled_contact in group_item["contacts"]:
                        uri = unicode(pickled_contact["uri"].strip())
                        contact = Contact(uri, group=group)
                        try:
                            contact.name = pickled_contact["display_name"]
                        except KeyError:
                            pass

                        try:
                            contact.preferred_media = pickled_contact["preferred_media"] if pickled_contact["preferred_media"] else None
                        except KeyError:
                            pass

                        try:
                            contact.save()
                        except DuplicateIDError:
                            pass
        unlink(path)

    def _NH_SIPAccountDidActivate(self, notification):
        if notification.sender is BonjourAccount():
            if self.bonjour_group not in self.groupsList:
                self.bonjour_group.setReference()
                positions = [g.position for g in AddressbookManager().get_groups() if g.position is not None]
                self.groupsList.insert(bisect.bisect_left(positions, self.bonjour_group.group.position), self.bonjour_group)
                self.nc.post_notification("BlinkContactsHaveChanged", sender=self, data=TimestampedNotificationData())
                self.nc.post_notification("BonjourGroupWasActivated", sender=self, data=TimestampedNotificationData())
        else:
            self.updatePresenceIndicator()

    def _NH_SIPAccountDidDeactivate(self, notification):
        if notification.sender is BonjourAccount() and self.bonjour_group in self.groupsList:
            self.bonjour_group.contacts = []
            self.groupsList.remove(self.bonjour_group)
            self.nc.post_notification("BonjourGroupWasDeactivated", sender=self, data=TimestampedNotificationData())
            self.nc.post_notification("BlinkContactsHaveChanged", sender=self, data=TimestampedNotificationData())

    def updatePresenceIndicator(self):
        return
        groups_with_presence = (group for group in self.groupsList if isinstance(group, BlinkPresenceContact))
        change = False
        # TODO: remove random import enable presence -adi
        import random
        for group in groups_with_presence:
            for blink_contact in group.contacts:
                    #continue

                    # TODO: set indicator to unknown when enable presence -adi
                    #blink_contact.setPresenceIndicator("unknown")
                    indicator = random.choice(('available','busy', 'activity', 'unknown'))
                    blink_contact.setPresenceIndicator(indicator)
                    activity = random.choice(PresenceStatusList)
                    if PresenceActivityPrefix.has_key(activity[1]):
                        detail = '%s %s %s' % (blink_contact.uri, PresenceActivityPrefix[activity[1]], activity[1])
                        blink_contact.setDetail(detail)
                    change = True

        if change:
            self.nc.post_notification("BlinkContactsHaveChanged", sender=self, data=TimestampedNotificationData())

    def _NH_BonjourAccountDidAddNeighbour(self, notification):
        neighbour = notification.data.neighbour
        display_name = notification.data.display_name
        host = notification.data.host
        uri = notification.data.uri
        BlinkLogger().log_info(u"Discovered new Bonjour neighbour: %s %s" % (display_name, uri))

        if neighbour not in (blink_contact.bonjour_neighbour for blink_contact in self.bonjour_group.not_filtered_contacts):
            blink_contact = BonjourBlinkContact(uri, neighbour, name='%s (%s)' % (display_name or 'Unknown', host))
            blink_contact.setPresenceIndicator("available")
            self.bonjour_group.not_filtered_contacts.append(blink_contact)

        if neighbour not in (blink_contact.bonjour_neighbour for blink_contact in self.bonjour_group.contacts):
            if uri.transport != 'tls':
                tls_neighbours = any(n for n in self.bonjour_group.contacts if n.aor.user == uri.user and n.aor.host == uri.host and n.aor.transport == 'tls')
                if not tls_neighbours:
                    blink_contact = BonjourBlinkContact(uri, neighbour, name='%s (%s)' % (display_name or 'Unknown', host))
                    blink_contact.setPresenceIndicator("available")
                    self.bonjour_group.contacts.append(blink_contact)
            else:
                blink_contact = BonjourBlinkContact(uri, neighbour, name='%s (%s)' % (display_name or 'Unknown', host))
                blink_contact.setPresenceIndicator("available")
                self.bonjour_group.contacts.append(blink_contact)
            non_tls_neighbours = [n for n in self.bonjour_group.contacts if n.aor.user == uri.user and n.aor.host == uri.host and n.aor.transport != 'tls']

            if uri.transport == 'tls':
                for n in non_tls_neighbours:
                    self.bonjour_group.contacts.remove(n)

            self.bonjour_group.sortContacts()
            self.nc.post_notification("BlinkContactsHaveChanged", sender=self, data=TimestampedNotificationData())

    def _NH_BonjourAccountDidUpdateNeighbour(self, notification):
        neighbour = notification.data.neighbour
        display_name = notification.data.display_name
        host = notification.data.host
        uri = notification.data.uri
        name = '%s (%s)' % (display_name or 'Unknown', host)

        BlinkLogger().log_info(u"Bonjour neighbour did change: %s %s" % (display_name, uri))
        try:
            blink_contact = (blink_contact for blink_contact in self.bonjour_group.contacts if blink_contact.bonjour_neighbour==neighbour).next()
        except StopIteration:
            blink_contact = BonjourBlinkContact(uri, neighbour, name='%s (%s)' % (display_name or 'Unknown', host))
            self.bonjour_group.not_filtered_contacts.append(blink_contact)
            if neighbour not in (blink_contact.bonjour_neighbour for blink_contact in self.bonjour_group.contacts):
                if uri.transport != 'tls':
                    tls_neighbours = any(n for n in self.bonjour_group.contacts if n.aor.user == uri.user and n.aor.host == uri.host and n.aor.transport == 'tls')
                    if not tls_neighbours:
                        blink_contact.setPresenceIndicator("unknown")
                        self.bonjour_group.contacts.append(blink_contact)
                else:
                    blink_contact.setPresenceIndicator("unknown")
                    self.bonjour_group.contacts.append(blink_contact)
        else:
            blink_contact.setName(name)
            blink_contact.setURI(str(uri))
            blink_contact.setDetail(str(uri))
            self.bonjour_group.sortContacts()
            self.nc.post_notification("BlinkContactsHaveChanged", sender=self, data=TimestampedNotificationData())

    def _NH_BonjourAccountDidRemoveNeighbour(self, notification):
        BlinkLogger().log_info(u"Bonjour neighbour removed: %s" % notification.data.neighbour.name)
        try:
            blink_contact = (blink_contact for blink_contact in self.bonjour_group.not_filtered_contacts if blink_contact.bonjour_neighbour==notification.data.neighbour).next()
        except StopIteration:
            pass
        else:
            self.bonjour_group.not_filtered_contacts.remove(blink_contact)

        try:
            blink_contact = (blink_contact for blink_contact in self.bonjour_group.contacts if blink_contact.bonjour_neighbour==notification.data.neighbour).next()
        except StopIteration:
            pass
        else:
            self.bonjour_group.contacts.remove(blink_contact)
            if blink_contact.aor.transport == 'tls':
                non_tls_neighbours = [n for n in self.bonjour_group.not_filtered_contacts if n.aor.user == blink_contact.aor.user and n.aor.host == blink_contact.aor.host and n.aor.transport != 'tls']
                for n in non_tls_neighbours:
                    self.bonjour_group.contacts.append(n)

            self.bonjour_group.sortContacts()
            self.nc.post_notification("BlinkContactsHaveChanged", sender=self, data=TimestampedNotificationData())

    def _NH_AddressbookContactWasActivated(self, notification):
        contact = notification.sender
        blink_contact = BlinkPresenceContact(contact)
        self.all_contacts_group.contacts.append(blink_contact)
        self.all_contacts_group.sortContacts()

        if not self.getBlinkGroupsForBlinkContact(blink_contact):
            self.no_group.contacts.append(blink_contact)
            self.no_group.sortContacts()

        self.nc.post_notification("BlinkContactsHaveChanged", sender=self, data=TimestampedNotificationData())

    def _NH_AddressbookContactWasDeleted(self, notification):
        contact = notification.sender
        self.removeContactFromBlinkGroups(contact, [self.favorites_group, self.all_contacts_group, self.no_group])
        self.nc.post_notification("BlinkContactsHaveChanged", sender=self, data=TimestampedNotificationData())

    def _NH_AddressbookContactDidChange(self, notification):
        contact = notification.sender

        for blink_contact in (blink_contact for blink_group in self.groupsList for blink_contact in blink_group.contacts if isinstance(blink_contact, BlinkPresenceContact) and blink_contact.contact == contact):
            if 'icon' in notification.data.modified:
                blink_contact.setIcon(loadContactIcon(contact))

            if 'uris' in notification.data.modified:
                blink_contact.setURIs(contact.uris)

            if 'name' in notification.data.modified or 'default_uri' in notification.data.modified:
                blink_contact.setURI(contact.default_uri)
                blink_contact.setDetail(contact.default_uri)
                blink_contact.setName(contact.name or contact.uri)


        [g.sortContacts() for g in self.groupsList]
        self.nc.post_notification("BlinkContactsHaveChanged", sender=self, data=TimestampedNotificationData())

    def _NH_AddressbookGroupWasActivated(self, notification):
        group = notification.sender
        settings = SIPSimpleSettings()

        positions = [g.position for g in AddressbookManager().get_groups() if g.position is not None and g.type != 'bonjour']
        positions.sort()
        index = bisect.bisect_left(positions, group.position)

        if group.type == "all_contacts":
            self.all_contacts_group.name = group.name
            if not group.position:
                position = len(self.groupsList) - 1 if self.groupsList else 0
                group.position = position
                group.save()
            self.groupsList.insert(index, self.all_contacts_group)

        elif group.type == "no_group":
            self.no_group.name = group.name
            if not group.position:
                position = len(self.groupsList) - 1 if self.groupsList else 0
                group.position = position
                group.save()
            for blink_contact in self.no_group.contacts:
                other_groups = self.getBlinkGroupsForBlinkContact(blink_contact)
                if other_groups:
                    try:
                        self.no_group.contacts.remove(blink_contact)
                    except ValueError:
                        pass
            self.groupsList.insert(index, self.no_group)

        elif group.type == "addressbook":
            self.addressbook_group.name = group.name
            if settings.contacts.enable_address_book:
                if not group.position:
                    position = len(self.groupsList) - 1 if self.groupsList else 0
                    group.position = position
                    group.save()
                self.addressbook_group.loadAddressBook()
                self.groupsList.insert(index, self.addressbook_group)

        elif group.type == "missed":
            self.missed_calls_group.name = group.name
            if NSApp.delegate().applicationName != 'Blink Lite' and settings.contacts.enable_missed_calls_group:
                if not group.position:
                    position = len(self.groupsList) - 1 if self.groupsList else 0
                    group.position = position
                    group.save()
                self.missed_calls_group.load_history()
                self.groupsList.insert(index, self.missed_calls_group)

        elif group.type == "outgoing":
            self.outgoing_calls_group.name = group.name
            if NSApp.delegate().applicationName != 'Blink Lite' and settings.contacts.enable_outgoing_calls_group:
                if not group.position:
                    position = len(self.groupsList) - 1 if self.groupsList else 0
                    group.position = position
                    group.save()
                self.outgoing_calls_group.load_history()
                self.groupsList.insert(index, self.outgoing_calls_group)

        elif group.type == "incoming":
            self.incoming_calls_group.name = group.name
            if NSApp.delegate().applicationName != 'Blink Lite' and settings.contacts.enable_incoming_calls_group:
                if not group.position:
                    position = len(self.groupsList) - 1 if self.groupsList else 0
                    group.position = position
                    group.save()
                self.incoming_calls_group.load_history()
                self.groupsList.insert(index, self.incoming_calls_group)

        elif group.type == "favorites":
            self.favorites_group.name = group.name
            if NSApp.delegate().applicationName != 'Blink Lite' and settings.contacts.enable_favorites_group:
                if not group.position:
                    position = len(self.groupsList) - 1 if self.groupsList else 0
                    group.position = position
                    group.save()
                self.groupsList.insert(index, self.favorites_group)
                for contact in group.contacts:
                    blink_contact = BlinkPresenceContact(contact)
                    self.favorites_group.contacts.append(blink_contact)
                    self.removeContactFromBlinkGroups(contact, [self.no_group])
                self.favorites_group.sortContacts()

        elif group.type is None:
            if not group.position:
                position = 0
                group.position = position
                group.save()
            blink_group = BlinkGroup(name=group.name, group=group)
            self.groupsList.insert(index, blink_group)
            for contact in group.contacts:
                blink_contact = BlinkPresenceContact(contact)
                blink_group.contacts.append(blink_contact)
                self.removeContactFromBlinkGroups(contact, [self.no_group])
            blink_group.sortContacts()

        self.nc.post_notification("BlinkContactsHaveChanged", sender=self, data=TimestampedNotificationData())
        self.nc.post_notification("AddressbookGroupsHaveChanged", sender=self, data=TimestampedNotificationData())

    def _NH_AddressbookGroupWasDeleted(self, notification):
        group = notification.sender
        try:
            blink_group = (grp for grp in self.groupsList if grp.group == group).next()
        except StopIteration:
            pass
        else:
            self.groupsList.remove(blink_group)
            blink_group.group = None
            self.saveGroupPosition()

            for blink_contact in blink_group.contacts:
                if not self.getBlinkGroupsForBlinkContact(blink_contact):
                    self.no_group.contacts.append(blink_contact)
                    self.no_group.sortContacts()

            self.nc.post_notification("BlinkContactsHaveChanged", sender=self, data=TimestampedNotificationData())
            self.nc.post_notification("AddressbookGroupsHaveChanged", sender=self, data=TimestampedNotificationData())

    def _NH_AddressbookGroupDidChange(self, notification):
        group = notification.sender
        try:
            blink_group = (grp for grp in self.groupsList if grp.group == group).next()
        except StopIteration:
            pass
        else:
            if 'contacts' in notification.data.modified:
                added = notification.data.modified['contacts'].added
                for contact in added:
                    try:
                        blink_contact = (blink_contact for blink_contact in blink_group.contacts if blink_contact.contact == contact).next()
                    except StopIteration:
                        blink_contact = BlinkPresenceContact(contact)
                        blink_group.contacts.append(blink_contact)
                        blink_group.sortContacts()
                        self.removeContactFromBlinkGroups(contact, [self.no_group])

                removed = notification.data.modified['contacts'].removed
                for contact in removed:
                    try:
                        blink_contact = (blink_contact for blink_contact in blink_group.contacts if blink_contact.contact == contact).next()
                    except StopIteration:
                        pass
                    else:
                        blink_group.contacts.remove(blink_contact)
                        blink_group.sortContacts()
                        if not self.getBlinkGroupsForBlinkContact(blink_contact):
                            blink_contact = BlinkPresenceContact(contact)
                            self.no_group.contacts.append(blink_contact)
                            self.no_group.sortContacts()

            elif 'name' in notification.data.modified:
                if blink_group.name != group.name:
                    blink_group.name = group.name
            self.nc.post_notification("BlinkContactsHaveChanged", sender=self, data=TimestampedNotificationData())
            self.nc.post_notification("AddressbookGroupsHaveChanged", sender=self, data=TimestampedNotificationData())

    def _NH_AddressbookGroupWasCreated(self, notification):
        self.saveGroupPosition()

    def isBlinkContactInBlinkGroups(self, contact, group):
        if not isinstance(contact, BlinkPresenceContact):
            return False
        for blink_contact in group.contacts:
            if blink_contact.contact is None:
                return False
            if blink_contact.contact.id == contact.contact.id:
                return True
        else:
            return False

    def getBlinkContactsForName(self, name):
        return (blink_contact for blink_contact in self.all_contacts_group.contacts if blink_contact.name == name)

    def getBlinkGroupsForBlinkContact(self, contact):
        found_groups = []
        all_blink_groups = (blink_group for blink_group in self.groupsList if blink_group.add_contact_allowed)
        for blink_group in all_blink_groups:
            for blink_contact in blink_group.contacts:
                if blink_contact.contact.id == contact.contact.id:
                    found_groups.append(blink_group)
                    break
        return found_groups

    def getGroupsForContact(self, search_contact):
        found_groups = []
        all_groups = (group for group in AddressbookManager.get_groups() if blink_group.type is None)
        for group in all_groups:
            for contact in group.contacts:
                if contact.contact.id == search_contact.id:
                    found_groups.append(group)
                    break
        return found_groups

    def saveGroupPosition(self):
        # save group expansion and position
        addressbook_manager = AddressbookManager()
        with addressbook_manager.transaction():
            for group in addressbook_manager.get_groups():
                try:
                    blink_group = (grp for grp in self.groupsList if grp.name == group.name).next()
                except StopIteration:
                    group.position = None
                    group.save()
                else:
                    if group.position != self.groupsList.index(blink_group):
                        group.position = self.groupsList.index(blink_group)
                        group.save()

    def createInitialGroupAndContacts(self):
        BlinkLogger().log_info(u"Creating initial contacts...")

        addressbook_manager = AddressbookManager()
        with addressbook_manager.transaction():
            group = Group(id='test')
            group.name = 'Test'
            group.expanded = True
            group.position = None

            test_contacts = {
                            "200901@login.zipdx.com":       { 'name': "VUC http://vuc.me", 'preferred_media': "audio", 'id': 'test_zipdx' },
                            "3333@sip2sip.info":            { 'name': "Call Test",         'preferred_media': "audio", 'id': 'test_audio' },
                            "4444@sip2sip.info":            { 'name': "Echo Test",         'preferred_media': "audio", 'id': 'test_microphone' },
                            "test@conference.sip2sip.info": { 'name': "Conference Test",   'preferred_media': "chat" , 'id': 'test_conference'}
                            }

            for uri in test_contacts.keys():
                icon = NSBundle.mainBundle().pathForImageResource_("%s.tiff" % uri)
                path = ApplicationData.get('photos/%s.tiff' % uri)
                NSFileManager.defaultManager().copyItemAtPath_toPath_error_(icon, path, None)

                contact = Contact(id=test_contacts[uri]['id'])
                contact.default_uri=uri
                contact.uris.add(ContactURI(uri=uri, type='SIP'))
                contact.name = test_contacts[uri]['name']
                contact.preferred_media = test_contacts[uri]['preferred_media']
                contact.save()
                group.contacts.add(contact)

            group.save()

            group = Group()
            group.name = 'Business'
            group.expanded = True
            group.save()

            group = Group()
            group.name = 'Family'
            group.expanded = True
            group.save()

            group = Group()
            group.name = 'Friends'
            group.expanded = True
            group.save()


    def moveBonjourGroupFirst(self):
        if self.bonjour_group in self.groupsList:
            self.groupsList.remove(self.bonjour_group)
            self.groupsList.insert(0, self.bonjour_group)
            self.saveGroupPosition()

    def restoreBonjourGroupPosition(self):
        if self.bonjour_group in self.groupsList and self.bonjour_group.group.position:
            self.groupsList.remove(self.bonjour_group)
            self.groupsList.insert(self.bonjour_group.group.position, self.bonjour_group)
            self.saveGroupPosition()

    def removeContactFromGroups(self, blink_contact, blink_groups):
        for blink_group in blink_groups:
            blink_group.group.contacts.remove(blink_contact.contact)
            blink_group.group.save()

    def removeContactFromBlinkGroups(self, contact, groups):
        for group in groups:
            try:
                blink_contact = next(blink_contact for blink_contact in group.contacts if blink_contact.contact == contact)
            except StopIteration:
                pass
            else:
                group.contacts.remove(blink_contact)

    def addGroup(self):
        controller = AddGroupController()
        name = controller.runModal()
        if not name or name in (blink_group.name for blink_group in self.groupsList):
            return

        group = Group()
        group.name = name
        group.expanded=True
        group.position=len(self.groupsList)-1 if self.groupsList else 0
        group.save()

    def editGroup(self, blink_group):
        controller = AddGroupController()
        name = controller.runModalForRename_(blink_group.name)
        if not name or name == blink_group.name:
            return
        blink_group.group.name = name
        blink_group.group.save()

    def addGroupsForContact(self, contact, groups):
        for grp in groups:
            try:
                group = next(g.group for g in self.groupsList if g == grp and g.add_contact_allowed)
            except StopIteration:
                # insert after last editable group
                for index, g in enumerate(self.groupsList):
                    if not g.add_contact_allowed:
                        break
                group = Group()
                group.name = grp
                group.position = index
            group.contacts.add(contact)
            group.save()

    def addContact(self, address="", group=None, name=None, type=None):
        if isinstance(address, SIPURI):
            address = address.user + "@" + address.host

        controller = AddContactController(uri=address, name=name, type=type)
        new_contact = controller.runModal()

        if not new_contact:
            return False

        addressbook_manager = AddressbookManager()
        with addressbook_manager.transaction():
            contact = Contact()
            contact.name = new_contact['name']
            contact.default_uri = new_contact['default_uri']
            contact.uris = new_contact['uris']
            contact.preferred_media = new_contact['preferred_media'] if new_contact['preferred_media'] else None
            icon = new_contact['icon']
            if icon is None:
                contact.icon = None
                saveContactIconToFile(None, contact.default_uri)
            else:
                contact.icon = base64Icon(icon)
            contact.presence.policy = new_contact['subscriptions']['presence']['policy']
            contact.presence.subscribe = new_contact['subscriptions']['presence']['subscribe']
            contact.dialog.policy = new_contact['subscriptions']['dialog']['policy']
            contact.dialog.subscribe = new_contact['subscriptions']['dialog']['subscribe']
            contact.save()
            self.addGroupsForContact(contact, new_contact['groups'] or [])
        return True

    def editContact(self, item):
        if not item:
            return

        if isinstance(item, SystemAddressBookBlinkContact):
            url = "addressbook://"+item.id
            NSWorkspace.sharedWorkspace().openURL_(NSURL.URLWithString_(url))
            return

        if not item.editable:
            return

        controller = EditContactController(item)
        new_contact = controller.runModal()
        if not new_contact:
            return

        addressbook_manager = AddressbookManager()
        with addressbook_manager.transaction():
            contact = item.contact
            contact.name = new_contact['name']
            contact.default_uri = new_contact['default_uri']
            contact.uris = new_contact['uris']
            contact.preferred_media = new_contact['preferred_media'] if new_contact['preferred_media'] else None
            icon = new_contact['icon']
            if icon is None:
                contact.icon = None
                saveContactIconToFile(None, contact.default_uri)
            else:
                contact.icon = base64Icon(icon)
            contact.presence.policy = new_contact['subscriptions']['presence']['policy']
            contact.presence.subscribe = new_contact['subscriptions']['presence']['subscribe']
            contact.dialog.policy = new_contact['subscriptions']['dialog']['policy']
            contact.dialog.subscribe = new_contact['subscriptions']['dialog']['subscribe']
            contact.save()

            old_groups = set(self.getBlinkGroupsForBlinkContact(item))
            new_groups = set(new_contact['groups'] or [])
            self.removeContactFromGroups(item, old_groups - new_groups)
            self.addGroupsForContact(contact, new_groups)

    def deleteContact(self, blink_contact):
        if not blink_contact.deletable:
            return

        name = blink_contact.name if len(blink_contact.name) else unicode(blink_contact.uri)
        message = u"Delete '%s' from the Contacts list?"%name
        message = re.sub("%", "%%", message)

        ret = NSRunAlertPanel(u"Delete Contact", message, u"Delete", u"Cancel", None)
        if ret == NSAlertDefaultReturn:
            blink_contact.contact.delete()
            self.nc.post_notification("BlinkContactsHaveChanged", sender=self, data=TimestampedNotificationData())

    def deleteGroup(self, blink_group):
        message = u"Please confirm the deletion of group '%s' from the Contacts list. The contacts part of this group will be preserved. "%blink_group.name
        message = re.sub("%", "%%", message)
        ret = NSRunAlertPanel(u"Delete Contact Group", message, u"Delete", u"Cancel", None)
        if ret == NSAlertDefaultReturn and blink_group in self.groupsList:
            if blink_group.deletable:
                blink_group.group.delete()
            self.nc.post_notification("BlinkContactsHaveChanged", sender=self, data=TimestampedNotificationData())
