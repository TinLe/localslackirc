#!/usr/bin/env python3
# localslackirc
# Copyright (C) 2018-2020 Salvo "LtWorf" Tomaselli
#
# localslackirc is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program.  If not, see <http://www.gnu.org/licenses/>.
#
# author Salvo "LtWorf" Tomaselli <tiposchi@tiscali.it>

import atexit
import datetime
from enum import Enum
from pathlib import Path
import re
import select
import signal
import socket
import argparse
from typing import *
import os
from os import environ
from os.path import expanduser
import pwd
from socket import gethostname
import sys
import traceback
import random

import slack
import rocket
from log import *


# How slack expresses mentioning users
_MENTIONS_REGEXP = re.compile(r'<@([0-9A-Za-z]+)>')
_CHANNEL_MENTIONS_REGEXP = re.compile(r'<#[A-Z0-9]+\|([A-Z0-9\-a-z]+)>')
_URL_REGEXP = re.compile(r'<([a-z0-9\-\.]+)://([^\s\|]+)[\|]{0,1}([^<>]*)>')
_RAND_REGEXP = re.compile(r'\.rand\s+([0-9]+)\s+([0-9]+)')


_SLACK_SUBSTITUTIONS = [
    ('&amp;', '&'),
    ('&gt;', '>'),
    ('&lt;', '<'),
]


class Replies(Enum):
    RPL_LUSERCLIENT = 251
    RPL_USERHOST = 302
    RPL_UNAWAY = 305
    RPL_NOWAWAY = 306
    RPL_WHOISUSER = 311
    RPL_WHOISSERVER = 312
    RPL_WHOISOPERATOR = 313
    RPL_ENDOFWHO = 315
    RPL_WHOISIDLE = 317
    RPL_ENDOFWHOIS = 318
    RPL_WHOISCHANNELS = 319
    RPL_LIST = 322
    RPL_LISTEND = 323
    RPL_CHANNELMODEIS = 324
    RPL_TOPIC = 332
    RPL_WHOREPLY = 352
    RPL_NAMREPLY = 353
    RPL_ENDOFNAMES = 366
    ERR_NOSUCHNICK = 401
    ERR_NOSUCHCHANNEL = 403
    ERR_UNKNOWNCOMMAND = 421
    ERR_FILEERROR = 424
    ERR_ERRONEUSNICKNAME = 432


class Provider(Enum):
    SLACK = 0
    ROCKETCHAT = 1


#: Inactivity days to hide a MPIM
MPIM_HIDE_DELAY = datetime.timedelta(days=50)


class Client:
    def __init__(self, s, sl_client: Union[slack.Slack, rocket.Rocket], nouserlist: bool, autojoin: bool, provider: Provider):
        self.nick = b''
        self.username = b''
        self.realname = b''
        self.parted_channels: Set[bytes] = set()
        self.hostname = gethostname().encode('utf8')

        self.provider = provider
        self.s = s
        self.sl_client = sl_client
        self.nouserlist = nouserlist
        self.autojoin = autojoin
        self._usersent = False  # Used to hold all events until the IRC client sends the initial USER message
        self._held_events: List[slack.SlackEvent] = []
        self._magic_users_id = 0
        self._magic_regex: Optional[re.Pattern] = None

        if self.provider == Provider.SLACK:
            self.substitutions = _SLACK_SUBSTITUTIONS
        else:
            self.substitutions = []

    def _nickhandler(self, cmd: bytes) -> None:
        _, nick = cmd.split(b' ', 1)
        self.nick = nick.strip()
        assert self.sl_client.login_info
        if self.nick != self.sl_client.login_info.self.name.encode('ascii'):
            self._sendreply(Replies.ERR_ERRONEUSNICKNAME, 'Incorrect nickname, use {}'.format(self.sl_client.login_info.self.name))
            # self._sendreply(Replies.ERR_ERRONEUSNICKNAME, 'Incorrect nickname, use %s' % self.sl_client.login_info.self.name)

    def _sendreply(self, code: Union[int, Replies], message: Union[str, bytes], extratokens: Iterable[Union[str, bytes]] = []) -> None:
        codeint = code if isinstance(code, int) else code.value
        bytemsg = message if isinstance(message, bytes) else message.encode('utf8')

        extratokens = list(extratokens)

        extratokens.insert(0, self.nick)

        try:
            self.s.send(b':%s %03d %s :%s\n' % (
                self.hostname,
                codeint,
                b' '.join(i if isinstance(i, bytes) else i.encode('utf8') for i in extratokens),
                bytemsg,
            ))
        except Exception as e:
            log("self.s.send(:%s %03d %s: %s) got Exception %s" % (
                self.hostname,
                codeint,
                b' '.join(i if isinstance(i, bytes) else i.encode('utf8') for i in extratokens),
                bytemsg,
                e
            ))
            return

    def _userhandler(self, cmd: bytes) -> None:
        #TODO USER salvo 8 * :Salvatore Tomaselli
        assert self.sl_client.login_info
        self._sendreply(1, 'Welcome to localslackirc')
        self._sendreply(2, 'Your team name is: %s' % self.sl_client.login_info.team.name)
        self._sendreply(2, 'Your team domain is: %s' % self.sl_client.login_info.team.domain)
        self._sendreply(2, 'Your nickname must be: %s' % self.sl_client.login_info.self.name)
        self._sendreply(Replies.RPL_LUSERCLIENT, 'There are 1 users and 0 services on 1 server')

        if self.autojoin and not self.nouserlist:
            # We're about to load many users for each chan; instead of requesting each
            # profile on its own, batch load the full directory.
            self.sl_client.prefetch_users()

        if self.autojoin:

            mpim_cutoff = datetime.datetime.utcnow() - MPIM_HIDE_DELAY

            for sl_chan in self.sl_client.channels():
                if not sl_chan.is_member:
                    continue

                if sl_chan.is_mpim and (sl_chan.latest is None or sl_chan.latest.timestamp < mpim_cutoff):
                    continue

                channel_name = '#%s' % sl_chan.name_normalized
                self._send_chan_info(channel_name.encode('utf-8'), sl_chan)
        else:
            for sl_chan in self.sl_client.channels():
                channel_name = '#%s' % sl_chan.name_normalized
                self.parted_channels.add(channel_name.encode('utf-8'))

        # Eventual channel joining done, sending the held events
        self._usersent = True
        for ev in self._held_events:
            self.slack_event(ev)
        self._held_events = []

    def _pinghandler(self, cmd: bytes) -> None:
        _, lbl = cmd.split(b' ', 1)
        self.s.send(b':%s PONG %s %s\n' % (self.hostname, self.hostname, lbl))

    def _joinhandler(self, cmd: bytes) -> None:
        _, channel_name_b = cmd.split(b' ', 1)

        if channel_name_b in self.parted_channels:
            self.parted_channels.remove(channel_name_b)

        channel_name = channel_name_b[1:].decode()
        try:
            slchan = self.sl_client.get_channel_by_name(channel_name)
        except Exception:
            self._sendreply(Replies.ERR_NOSUCHCHANNEL, f'Unable to find channel: {channel_name}')
            return

        if not slchan.is_member:
            try:
                self.sl_client.join(slchan)
            except Exception:
                self._sendreply(Replies.ERR_NOSUCHCHANNEL, f'Unable to join server channel: {channel_name}')

        try:
            self._send_chan_info(channel_name_b, slchan)
        except Exception:
            self._sendreply(Replies.ERR_NOSUCHCHANNEL, f'Unable to join channel: {channel_name}')

    def _send_chan_info(self, channel_name: bytes, slchan: slack.Channel):
        if not self.nouserlist:
            userlist: List[bytes] = []
            for i in self.sl_client.get_members(slchan.id):
                try:
                    u = self.sl_client.get_user(i)
                except Exception:
                    continue
                if u.deleted:
                    # Disabled user, skip it
                    continue
                name = u.name.encode('utf8')
                prefix = b'@' if u.is_admin else b''
                userlist.append(prefix + name)

            users = b' '.join(userlist)

        # self.s.send(b':%s!salvo@127.0.0.1 JOIN %s\n' % (self.nick, channel_name))
        self.s.send(b':%s!%s@127.0.0.1 JOIN %s\n' % (self.nick, self.nick, channel_name))
        self._sendreply(Replies.RPL_TOPIC, slchan.real_topic, [channel_name])
        self._sendreply(Replies.RPL_NAMREPLY, b'' if self.nouserlist else users, ['=', channel_name])
        self._sendreply(Replies.RPL_ENDOFNAMES, 'End of NAMES list', [channel_name])

    def _privmsghandler(self, cmd: bytes) -> None:
        _, dest, msg = cmd.split(b' ', 2)
        if msg.startswith(b':'):
            msg = msg[1:]

        # Handle sending "/me does something"
        # b'PRIVMSG #much_private :\x01ACTION saluta tutti\x01'
        if msg.startswith(b'\x01ACTION ') and msg.endswith(b'\x01'):
            action = True
            _, msg = msg.split(b' ', 1)
            msg = msg[:-1]
        else:
            action = False

        message = self._addmagic(msg.decode('utf8'))

        if dest.startswith(b'#'):
            self.sl_client.send_message(
                self.sl_client.get_channel_by_name(dest[1:].decode()).id,
                message,
                action,
            )
        else:
            try:
                self.sl_client.send_message_to_user(
                    self.sl_client.get_user_by_name(dest.decode()).id,
                    message,
                    action,
                )
            except Exception:
                log('Impossible to find user ', dest)

    def _listhandler(self, cmd: bytes) -> None:
        for c in self.sl_client.channels(refresh=True):
            self._sendreply(Replies.RPL_LIST, c.real_topic, ['#' + c.name, str(c.num_members)])
        self._sendreply(Replies.RPL_LISTEND, 'End of LIST')

    def _modehandler(self, cmd: bytes) -> None:
        params = cmd.split(b' ', 2)
        self._sendreply(Replies.RPL_CHANNELMODEIS, '', [params[1], '+'])

    def _sendfilehandler(self, cmd: bytes) -> None:
        #/sendfile #destination filename
        params = cmd.split(b' ', 2)
        try:
            channel_name = params[1].decode('utf8')
            filename = params[2].decode('utf8')
        except IndexError:
            self._sendreply(Replies.ERR_UNKNOWNCOMMAND, 'Syntax: /sendreply #channel filename')
            return

        try:
            if channel_name.startswith('#'):
                dest = self.sl_client.get_channel_by_name(channel_name[1:]).id
            else:
                dest = self.sl_client.get_user_by_name(channel_name).id
        except KeyError:
            self._sendreply(Replies.ERR_NOSUCHCHANNEL, f'Unable to find destination: {channel_name}')
            return

        try:
            self.sl_client.send_file(dest, filename)
            self._sendreply(0, 'Upload of %s completed' % filename)
        except Exception as e:
            self._sendreply(Replies.ERR_FILEERROR, f'Unable to send file {e}')

    def _parthandler(self, cmd: bytes) -> None:
        _, name = cmd.split(b' ', 1)
        self.parted_channels.add(name)

    def _awayhandler(self, cmd: bytes) -> None:
        is_away = b' ' in cmd
        self.sl_client.away(is_away)
        response = Replies.RPL_NOWAWAY if is_away else Replies.RPL_UNAWAY
        self._sendreply(response, 'Away status changed')

    def _topichandler(self, cmd: bytes) -> None:
        _, channel_b, topic_b = cmd.split(b' ', 2)
        topic = topic_b.decode()[1:]
        channel = self.sl_client.get_channel_by_name(channel_b.decode()[1:])
        try:
            self.sl_client.topic(channel, topic)
        except Exception:
            self._sendreply(Replies.ERR_UNKNOWNCOMMAND, f'Unable to set topic to {topic}')

    def _whoishandler(self, cmd: bytes) -> None:
        users = cmd.split(b' ')
        del users[0]

        if len(users) > 1:
            self._sendreply(Replies.ERR_UNKNOWNCOMMAND, 'Server parameter is not supported')

        # Seems that oftc only responds to the last one
        username = users.pop()

        if b'*' in username:
            self._sendreply(Replies.ERR_UNKNOWNCOMMAND, 'Wildcards are not supported')
        uusername = username.decode()
        try:
            user = self.sl_client.get_user_by_name(uusername)
        except KeyError:
            self._sendreply(Replies.ERR_NOSUCHNICK, f'Unknown user {uusername}')

        self._sendreply(Replies.RPL_WHOISUSER, user.real_name, [username, '', 'localhost'])
        if user.profile.email:
            self._sendreply(Replies.RPL_WHOISUSER, f'email: {user.profile.email}', [username, '', 'localhost'])
        if user.is_admin:
            self._sendreply(Replies.RPL_WHOISOPERATOR, f'{uusername} is an IRC operator', [username])
        self._sendreply(Replies.RPL_ENDOFWHOIS, '', extratokens=[username])

    def _kickhandler(self, cmd: bytes) -> None:
        _, channel_b, username, message = cmd.split(b' ', 3)
        channel = self.sl_client.get_channel_by_name(channel_b.decode()[1:])
        user = self.sl_client.get_user_by_name(username.decode())
        try:
            self.sl_client.kick(channel, user)
        except Exception as e:
            self._sendreply(Replies.ERR_UNKNOWNCOMMAND, 'Error: %s' % e)

    def _userhosthandler(self, cmd: bytes) -> None:
        nicknames = cmd.split(b' ')
        del nicknames[0]  # Remove the command itself
        #TODO replace + with - in case of away
        #TODO append a * to the nickname for OP

        replies = (b'%s=+unknown' % i for i in nicknames)
        self._sendreply(Replies.RPL_USERHOST, '', replies)

    def _invitehandler(self, cmd: bytes) -> None:
        _, username, channel_b = cmd.split(b' ', 2)
        channel = self.sl_client.get_channel_by_name(channel_b.decode()[1:])
        user = self.sl_client.get_user_by_name(username.decode())
        try:
            self.sl_client.invite(channel, user)
        except Exception as e:
            self._sendreply(Replies.ERR_UNKNOWNCOMMAND, 'Error: %s' % e)

    def _whohandler(self, cmd: bytes) -> None:
        _, name = cmd.split(b' ', 1)
        if not name.startswith(b'#'):
            try:
                user = self.sl_client.get_user_by_name(name.decode())
            except KeyError:
                return
            self._sendreply(Replies.RPL_WHOREPLY, '0 %s' % user.real_name, [name, user.name, '127.0.0.1', self.hostname, user.name, 'H'])
            return

        try:
            channel = self.sl_client.get_channel_by_name(name.decode()[1:])
        except KeyError:
            return

        for i in self.sl_client.get_members(channel.id):
            try:
                user = self.sl_client.get_user(i)
                self._sendreply(Replies.RPL_WHOREPLY, '0 %s' % user.real_name, [name, user.name, '127.0.0.1', self.hostname, user.name, 'H'])
            except Exception:
                pass
        self._sendreply(Replies.RPL_ENDOFWHO, 'End of WHO list', [name])

    def sendmsg(self, from_: bytes, to: bytes, message: bytes) -> None:
        # self.s.send(b':%s!salvo@127.0.0.1 PRIVMSG %s :%s\n' % (
        self.s.send(b':%s!%s@127.0.0.1 PRIVMSG %s :%s\n' % (
            from_,
            self.nick,
            to,  # private message, or a channel
            message,
        ))

    def _addmagic(self, msg: str) -> str:
        """
        Adds magic codes and various things to
        outgoing messages
        """
        for i in self.substitutions:
            msg = msg.replace(i[1], i[0])
        if self.provider == Provider.SLACK:
            msg = msg.replace('@here', '<!here>')
            msg = msg.replace('@channel', '<!channel>')
            msg = msg.replace('@everyone', '<!everyone>')
        elif self.provider == Provider.ROCKETCHAT:
            msg = msg.replace('@yell', '@channel')
            msg = msg.replace('@shout', '@channel')
            msg = msg.replace('@attention', '@channel')

        # Extremely inefficient code to generate mentions
        # Just doing them client-side on the receiving end is too mainstream

        if self._magic_users_id == id(self.sl_client.get_usernames()):
            regex = self._magic_regex
            assert regex
        else:
            usernames = self.sl_client.get_usernames()
            assert usernames
            self._magic_users_id = id(usernames)
            regexs = (r'((://\S*){0,1}\b%s\b)' % username for username in usernames)
            regex = re.compile('|'.join(regexs))
            self._magic_regex = regex

        matches = list(re.finditer(regex, msg))
        matches.reverse()  # I want to replace from end to start or the positions get broken
        for m in matches:
            username = m.string[m.start():m.end()]
            if username.startswith('://'):
                continue  # Match inside a url
            elif self.provider == Provider.SLACK:
                msg = msg[0:m.start()] + '<@%s>' % self.sl_client.get_user_by_name(username).id + msg[m.end():]
            elif self.provider == Provider.ROCKETCHAT:
                msg = msg[0:m.start()] + f'@{username}' + msg[m.end():]
        return msg

    def parse_message(self, msg: str) -> Iterator[bytes]:
        log("parse_message: msg=({}) ".format(msg))
        for i in msg.split('\n'):
            log("parse_message: i=({}) ".format(i))
            if not i:
                continue

            """
            Add in bot stuff here.
            ===============================================================
            bot_rand = _RAND_REGEXP.search(i)
            if bot_rand:
                rnd1, rnd2 = bot_rand.groups()
                rndnum = random.randrange(int(rnd1), int(rnd2))
                log("got a .rand request; rnd1={}, rnd2={}, rndnum={}".format(rnd1, rnd2, rndnum))
                i = ".rand {} {} = {}".format(int(rnd1), int(rnd2), rndnum)
                encoded = i.encode('utf8')
                yield encoded
            ===============================================================
            """

            # Replace all mentions with @user
            while True:
                mention = _MENTIONS_REGEXP.search(i)
                if not mention:
                    break
                i = (
                    i[0:mention.span()[0]] +
                    self.sl_client.get_user(mention.groups()[0]).name +
                    i[mention.span()[1]:]
                )

            # Replace all channel mentions
            if self.provider == Provider.SLACK:
                while True:
                    mention = _CHANNEL_MENTIONS_REGEXP.search(i)
                    if not mention:
                        break
                    i = (
                        i[0:mention.span()[0]] +
                        '#' +
                        mention.groups()[0] +
                        i[mention.span()[1]:]
                    )

                while True:
                    url = _URL_REGEXP.search(i)
                    if not url:
                        break
                    schema, path, label = url.groups()
                    i = (
                        i[0:url.span()[0]] +
                        f'{schema}://{path}' +
                        (f' ({label})' if label else '') +
                        i[url.span()[1]:]
                    )

            for s in self.substitutions:
                i = i.replace(s[0], s[1])

            encoded = i.encode('utf8')

            if self.provider == Provider.SLACK:
                encoded = encoded.replace(b'<!here>', b'yelling [%s]' % self.nick)
                encoded = encoded.replace(b'<!channel>', b'YELLING LOUDER [%s]' % self.nick)
                encoded = encoded.replace(b'<!everyone>', b'DEAFENING YELL [%s]' % self.nick)
            elif self.provider == Provider.ROCKETCHAT:
                encoded = encoded.replace(b'@here', b'yelling [%s]' % self.nick)
                encoded = encoded.replace(b'@channel', b'YELLING LOUDER [%s]' % self.nick)

            log("leaving parse_message encoded={}".format(encoded))
            yield encoded

    def _message(self, sl_ev: Union[slack.Message, slack.MessageDelete, slack.MessageBot, slack.ActionMessage], prefix: str = ''):
        """
        Sends a message to the irc client
        """
        if hasattr(sl_ev, 'user'):
            source = self.sl_client.get_user(sl_ev.user).name.encode('utf8')  # type: ignore
        else:
            source = b'bot'
        try:
            dest = b'#' + self.sl_client.get_channel(sl_ev.channel).name.encode('utf8')
        except KeyError:
            dest = self.nick
        except Exception as e:
            log('_message Error: ', str(e))
            return
        if dest in self.parted_channels:
            # Ignoring messages, channel was left on IRC
            return
        for msg in self.parse_message(prefix + sl_ev.text):
            if isinstance(sl_ev, slack.ActionMessage):
                msg = b'\x01ACTION ' + msg + b'\x01'
            self.sendmsg(
                source,
                dest,
                msg
            )

    def _joined_parted(self, sl_ev: Union[slack.Join, slack.Leave], joined: bool) -> None:
        """
        Handle join events from slack, by sending a JOIN notification
        to IRC.
        """
        user = self.sl_client.get_user(sl_ev.user)
        if user.deleted:
            return
        try:
            channel = self.sl_client.get_channel(sl_ev.channel)
            dest = b'#' + channel.name.encode('utf8')
            if dest in self.parted_channels:
                return
            name = user.name.encode('utf8')
            rname = user.real_name.replace(' ', '_').encode('utf8')
            if joined:
                self.s.send(b':%s!%s@127.0.0.1 JOIN :%s\n' % (name, rname, dest))
            else:
                self.s.send(b':%s!%s@127.0.0.1 PART %s\n' % (name, rname, dest))
        except Exception as e:
            log("_joined_parted: channel {} is probably PRIVATE or threaded conversation!".format(channel))

    def slack_event(self, sl_ev: slack.SlackEvent) -> None:
        if not self._usersent:
            self._held_events.append(sl_ev)
            return
        if isinstance(sl_ev, slack.MessageDelete):
            self._message(sl_ev, '[deleted]')
        elif isinstance(sl_ev, slack.Message):
            self._message(sl_ev)
        elif isinstance(sl_ev, slack.ActionMessage):
            self._message(sl_ev)
        elif isinstance(sl_ev, slack.MessageEdit):
            if sl_ev.is_changed:
                self._message(sl_ev.diffmsg)
        elif isinstance(sl_ev, slack.MessageBot):
            self._message(sl_ev, '[%s]' % sl_ev.username)
        elif isinstance(sl_ev, slack.FileShared):
            f = self.sl_client.get_file(sl_ev)
            self._message(f.announce())
        elif isinstance(sl_ev, slack.Join):
            self._joined_parted(sl_ev, True)
        elif isinstance(sl_ev, slack.Leave):
            self._joined_parted(sl_ev, False)
        elif isinstance(sl_ev, slack.TopicChange):
            self._sendreply(Replies.RPL_TOPIC, sl_ev.topic, ['#' + self.sl_client.get_channel(sl_ev.channel).name])
        elif isinstance(sl_ev, slack.GroupJoined):
            channel_name = '#%s' % sl_ev.channel.name_normalized
            self._send_chan_info(channel_name.encode('utf-8'), sl_ev.channel)

    def command(self, cmd: bytes) -> None:
        if b' ' in cmd:
            cmdid, _ = cmd.split(b' ', 1)
        else:
            cmdid = cmd

        handlers = {
            b'NICK': self._nickhandler,
            b'USER': self._userhandler,
            b'PING': self._pinghandler,
            b'JOIN': self._joinhandler,
            b'PRIVMSG': self._privmsghandler,
            b'LIST': self._listhandler,
            b'WHO': self._whohandler,
            b'MODE': self._modehandler,
            b'PART': self._parthandler,
            b'AWAY': self._awayhandler,
            b'TOPIC': self._topichandler,
            b'KICK': self._kickhandler,
            b'INVITE': self._invitehandler,
            b'sendfile': self._sendfilehandler,
            #QUIT
            #CAP LS
            b'USERHOST': self._userhosthandler,
            b'whois': self._whoishandler,
        }

        if cmdid in handlers:
            handlers[cmdid](cmd)
        else:
            self._sendreply(Replies.ERR_UNKNOWNCOMMAND, 'Unknown command', [cmdid])
            log('Unknown command: ', cmd)


def exit_hook(status_file, sl_client) -> None:
    if status_file:
        log(f'Writing status to {status_file}')
        status_file.write_bytes(sl_client.get_status())
    log('Exiting...')


def su() -> None:
    """
    switch user. Useful when starting localslackirc
    as a service as root user.
    """
    if sys.platform.startswith('win'):
        return

    # Nothing to do, already not root
    if os.getuid() != 0:
        return

    username = environ.get('PROCESS_OWNER', 'nobody')
    userdata = pwd.getpwnam(username)
    os.setgid(userdata.pw_gid)
    os.setegid(userdata.pw_gid)
    os.setuid(userdata.pw_uid)
    os.seteuid(userdata.pw_uid)


def main() -> None:
    su()

    parser = argparse.ArgumentParser()
    parser.add_argument('-p', '--port', type=int, action='store', dest='port',
                        default=9007, required=False,
                        help='set port number. Defaults to 9007')
    parser.add_argument('-i', '--ip', type=str, action='store', dest='ip',
                        default='127.0.0.1', required=False,
                        help='set ip address')
    parser.add_argument('-t', '--tokenfile', type=str, action='store', dest='tokenfile',
                        default=expanduser('~')+'/.localslackirc',
                        required=False,
                        help='set the token file')
    parser.add_argument('-c', '--cookiefile', type=str, action='store', dest='cookiefile',
                        default=None,
                        required=False,
                        help='set the cookie file (for slack only, for xoxc tokens)')
    parser.add_argument('-u', '--nouserlist', action='store_true',
                        dest='nouserlist', required=False,
                        help='don\'t display userlist')
    parser.add_argument('-j', '--autojoin', action='store_true',
                        dest='autojoin', required=False,
                        help="Automatically join all remote channels")
    parser.add_argument('-o', '--override', action='store_true',
                        dest='overridelocalip', required=False,
                        help='allow non 127. addresses, this is potentially dangerous')
    parser.add_argument('--rc-url', type=str, action='store', dest='rc_url', default=None, required=False,
                        help='The rocketchat URL. Setting this changes the mode from slack to rocketchat')
    parser.add_argument('-f', '--status-file', type=str, action='store', dest='status_file', required=False, default=None,
                        help='Path to the file to keep the internal status.')
    parser.add_argument('--log-suffix', type=str, action='store', dest='log_suffix', default='',
                        help='Set a suffix for the syslog identifier')

    args = parser.parse_args()

    openlog(environ.get('LOG_SUFFIX', args.log_suffix))

    status_file_str: Optional[str] = environ.get('STATUS_FILE', args.status_file)
    status_file = None
    if status_file_str is not None:
        log('Status file at:', status_file_str)
        status_file = Path(status_file_str)

    ip: str = environ.get('IP_ADDRESS', args.ip)
    overridelocalip: bool = environ['OVERRIDE_LOCAL_IP'].lower() == 'true' if 'OVERRIDE_LOCAL_IP' in environ else args.overridelocalip

    # Exit if their chosden ip isn't local. User can override with -o if they so dare
    if not ip.startswith('127') and not overridelocalip:
        exit('supplied ip isn\'t local\nlocalslackirc has no encryption or ' \
                'authentication, it\'s recommended to only allow local connections\n' \
                'you can override this with -o')

    port = int(environ.get('PORT', args.port))
    rc_url: Optional[str] = environ.get('RC_URL', args.rc_url)

    autojoin: bool = environ['AUTOJOIN'].lower() == 'true' if 'AUTOJOIN' in environ else args.autojoin
    nouserlist: bool = environ['NOUSERLIST'].lower() == 'true' if 'NOUSERLIST' in environ else args.nouserlist

    if 'TOKEN' in environ:
        token = environ['TOKEN']
    else:
        try:
            with open(args.tokenfile) as f:
                token = f.readline().strip()
        except IsADirectoryError:
            exit(f'Not a file {args.tokenfile}')
        except (FileNotFoundError, PermissionError):
            exit(f'Unable to open the token file {args.tokenfile}')

    if 'COOKIE' in environ:
        cookie: Optional[str] = environ['COOKIE']
    else:
        try:
            if args.cookiefile:
                with open(args.cookiefile) as f:
                    cookie = f.readline().strip()
            else:
                cookie = None
        except (FileNotFoundError, PermissionError):
            exit(f'Unable to open the cookie file {args.cookiefile}')
        except IsADirectoryError:
            exit(f'Not a file {args.cookiefile}')

    if token.startswith('xoxc-') and not cookie:
        exit('The cookie is needed for this kind of slack token')

    previous_status = None
    if status_file is not None and status_file.exists():
        previous_status = status_file.read_bytes()

    if rc_url:
        sl_client: Union[slack.Slack, rocket.Rocket] = rocket.Rocket(rc_url, token, previous_status)
        provider = Provider.ROCKETCHAT
    else:
        sl_client = slack.Slack(token, cookie, previous_status)
        provider = Provider.SLACK

    atexit.register(exit_hook, status_file, sl_client)
    term_f = lambda _, __: sys.exit(0)
    signal.signal(signal.SIGHUP, term_f)
    signal.signal(signal.SIGTERM, term_f)

    sl_events = sl_client.events_iter()
    serversocket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    serversocket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    serversocket.bind((ip, port))
    serversocket.listen(1)

    poller = select.poll()

    while True:
        s, _ = serversocket.accept()
        ircclient = Client(s, sl_client, nouserlist, autojoin, provider)

        poller.register(s.fileno(), select.POLLIN)
        if sl_client.fileno is not None:
            poller.register(sl_client.fileno, select.POLLIN)

        # Main loop
        timeout = 2
        while True:
            s_event: List[Tuple[int, int]] = poller.poll(timeout)
            sl_event = next(sl_events)

            if s_event:
                text = s.recv(1024)
                if len(text) == 0:
                    break
                #FIXME handle the case when there is more to be read
                for i in text.split(b'\n')[:-1]:
                    i = i.strip()
                    if i:
                        ircclient.command(i)

            while sl_event:
                log("in sl_event loop...")
                ircclient.slack_event(sl_event)
                sl_event = next(sl_events)


if __name__ == '__main__':
    while True:
        try:
            main()
        except KeyboardInterrupt:
            break
        except Exception as e:
            traceback.print_last()
            pass
