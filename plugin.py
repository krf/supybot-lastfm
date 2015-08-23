###
# Copyright (c) 2006, Ilya Kuznetsov
# Copyright (c) 2008,2012 Kevin Funk
# All rights reserved.
#
# Redistribution and use in source and binary forms, with or without
# modification, are permitted provided that the following conditions are met:
#
#   * Redistributions of source code must retain the above copyright notice,
#     this list of conditions, and the following disclaimer.
#   * Redistributions in binary form must reproduce the above copyright notice,
#     this list of conditions, and the following disclaimer in the
#     documentation and/or other materials provided with the distribution.
#   * Neither the name of the author of this software nor the name of
#     contributors to this software may be used to endorse or promote products
#     derived from this software without specific prior written consent.
#
# THIS SOFTWARE IS PROVIDED BY THE COPYRIGHT HOLDERS AND CONTRIBUTORS "AS IS"
# AND ANY EXPRESS OR IMPLIED WARRANTIES, INCLUDING, BUT NOT LIMITED TO, THE
# IMPLIED WARRANTIES OF MERCHANTABILITY AND FITNESS FOR A PARTICULAR PURPOSE
# ARE DISCLAIMED.  IN NO EVENT SHALL THE COPYRIGHT OWNER OR CONTRIBUTORS BE
# LIABLE FOR ANY DIRECT, INDIRECT, INCIDENTAL, SPECIAL, EXEMPLARY, OR
# CONSEQUENTIAL DAMAGES (INCLUDING, BUT NOT LIMITED TO, PROCUREMENT OF
# SUBSTITUTE GOODS OR SERVICES; LOSS OF USE, DATA, OR PROFITS; OR BUSINESS
# INTERRUPTION) HOWEVER CAUSED AND ON ANY THEORY OF LIABILITY, WHETHER IN
# CONTRACT, STRICT LIABILITY, OR TORT (INCLUDING NEGLIGENCE OR OTHERWISE)
# ARISING IN ANY WAY OUT OF THE USE OF THIS SOFTWARE, EVEN IF ADVISED OF THE
# POSSIBILITY OF SUCH DAMAGE.

###

import supybot.utils as utils
from supybot.commands import *
import supybot.conf as conf
import supybot.plugins as plugins
import supybot.ircutils as ircutils
import supybot.callbacks as callbacks
import supybot.world as world
import supybot.log as log
import json
import urllib
from lxml import html
import requests
import string

import urllib2
from xml.dom import minidom
from time import time
from time import mktime
from datetime import datetime

from LastFMDB import *

class LastFMParser:

    def parseRecentTracks(self, stream):
        """
        @return Tuple with track information of last track
        """

        xml = minidom.parse(stream).getElementsByTagName("recenttracks")[0]
        user = xml.getAttribute("user")

        try:
            t = xml.getElementsByTagName("track")[0] # most recent track
        except IndexError:
            return [user] + [None]*5
        isNowPlaying = (t.getAttribute("nowplaying") == "true")
        if not isNowPlaying:
            time = int(t.getElementsByTagName("date")[0].getAttribute("uts"))
        else:
            time = None

        artist = t.getElementsByTagName("artist")[0].firstChild.data
        track = t.getElementsByTagName("name")[0].firstChild.data
        try:
            albumNode = t.getElementsByTagName("album")[0].firstChild
            album = albumNode.data
        except (IndexError, AttributeError):
            album = None
        return (user, isNowPlaying, artist, track, album, time)
        
class NewLastFMParser:

    def parseRecentTracks(self, stream):
        """
        @return Tuple with track information of last track
        """
        # Implemented to replace broken API after LastFM site update.
        # Unfortunately this method (scraping last.fm profile page)
        #  does not include album.

        tree = html.fromstring(stream)
        user = tree.xpath('//*[@id="content"]/header/div[2]/div/div[2]/div[1]/h1/text()')
        user = filter(lambda x: x in string.printable, ''.join(user).strip())
        track = tree.xpath('//*[@id="recent-tracks-section"]/table/tbody/tr[1]/td[3]/span/a/text()')
        track = filter(lambda x: x in string.printable, ''.join(track).strip())
        artist = tree.xpath('//*[@id="recent-tracks-section"]/table/tbody/tr[1]/td[3]/span/span[2]/a/text()')
        artist = filter(lambda x: x in string.printable, ''.join(artist).strip())
        timestamp = tree.xpath('//*[@id="recent-tracks-section"]/table/tbody/tr[1]/td[4]/span/text()')
        timestamp = ''.join(timestamp).strip()

        if timestamp == "Scrobbling now":
            isNowPlaying = True
            time = timestamp
        else:
            time = timestamp
            isNowPlaying = False

        return (user, isNowPlaying, artist, track, time)

class LastFM(callbacks.Plugin):

    def __init__(self, irc):
        self.__parent = super(LastFM, self)
        self.__parent.__init__(irc)
        self.db = LastFMDB(dbfilename)
        world.flushers.append(self.db.flush)
        # 1.0 API (deprecated)
        self.APIURL_1_0 = "http://ws.audioscrobbler.com/1.0/user"

        # 2.0 API (see http://www.lastfm.de/api/intro)
        self.APIKEYLM = self.registryValue("apiKeyLastFM")
        self.APIURL_2_0 = "http://ws.audioscrobbler.com/2.0/?api_key=%s&" % self.APIKEYLM

        # YouTube API
        self.APIKEYYT = self.registryValue("apiKeyYouTube")

    def die(self):
        if self.db.flush in world.flushers:
            world.flushers.remove(self.db.flush)
        self.db.close()
        self.__parent.die()
        
    def _yt(self, query):
        """
        @return link based on now playing results
        """
        if not self.APIKEYYT:
            self.log.error("LastFM._yt: [ERR] registryValue(\"apiKeyYouTube\") not set.")
            isKeySet = "(is the API key set?)"
        else:
            isKeySet = ""

        url = "https://www.googleapis.com/youtube/v3/search"
        key = self.APIKEYYT
        noresults = "No YouTube results found. %s" % isKeySet

        opts = {"q": query,
                "part": "snippet",
                "maxResults": 1,
                "order": "relevance",
                "key": key,
                "safeSearch": "none"}

        search_url = "%s?%s" % (url, urllib.urlencode(opts))    
        result = False
        
        try:
            response = utils.web.getUrl(search_url).decode("utf8")
            data = json.loads(response)
            
            try:
                items = data["items"]
                
                if items:
                    video = items[0]
                    snippet = video["snippet"]
                    vid = video["id"]["videoId"]
                    title = snippet["title"]
                    result = True                    
                    link = "https://youtu.be/%s" % (vid)
                else:
                    self.log.error("LastFM._yt: [ERR] unexpected API response")
            
            except IndexError as e:
                self.log.error("LastFM._yt: [ERR] unexpected API response (%s)" % str(e))

        except Exception as err:
            self.log.error("LastFM._yt: [ERR] unexpected API response (%s)" % str(err))
        
        if result:
            self.log.debug("LastFM._yt: [URL] %s" % link)
            return link
        else:
            self.log.debug("LastFM._yt: [URL] %s" % noresults)
            return noresults

    def lastfm(self, irc, msg, args, method, optionalId):
        """<method> [<id>]

        Lists LastFM info where <method> is in
        [friends, neighbours, profile, recenttracks, tags, topalbums,
        topartists, toptracks].
        Set your LastFM ID with the set method (default is your current nick)
        or specify <id> to switch for one call.
        """

        id = (optionalId or self.db.getId(msg.nick) or msg.nick)
        channel = msg.args[0]
        maxResults = self.registryValue("maxResults", channel)
        method = method.lower()

        url = "%s/%s/%s.txt" % (self.APIURL_1_0, id, method)
        try:
            f = urllib2.urlopen(url)
        except urllib2.HTTPError:
            irc.error("Unknown ID (%s) or unknown method (%s)"
                    % (msg.nick, method))
            return


        lines = f.read().split("\n")
        content = map(lambda s: s.split(",")[-1], lines)

        irc.reply("%s's %s: %s (with a total number of %i entries)"
                % (id, method, ", ".join(content[0:maxResults]),
                    len(content)))

    lastfm = wrap(lastfm, ["something", optional("something")])

    def nowPlaying(self, irc, msg, args, optionalId):
        """[<id>]

        Announces the now playing track of the specified LastFM ID.
        Set your LastFM ID with the set method (default is your current nick)
        or specify <id> to switch for one call.
        """

        # check to see if optionalId has been registered with the database
        if optionalId:
            try:
                uid = self.db.getId(optionalId)
                if not uid:
                    uid = optionalId
                    self.log.debug("LastFM.nowPlaying: [INFO] optionalId not converted, using: %s" % uid)
                else:
                    self.log.debug("LastFM.nowPlaying: [INFO] optionalId converted to LastFM id: %s" % uid)
            except:
                uid = optionalId
                self.log.debug("LastFM.nowPlaying: [INFO] optionalId not converted, using: %s" % uid)
        else:
            uid = (self.db.getId(msg.nick) or msg.nick)
            self.log.debug("LastFM.nowPlaying: [INFO] optionalId not given, using: %s" % uid)

        # see http://www.lastfm.de/api/show/user.getrecenttracks
        # url = "%s&method=user.getrecenttracks&user=%s" % (self.APIURL_2_0, id)
        url = "http://www.last.fm/user/%s" % (uid)
        try:
            f = utils.web.getUrl(url).decode('utf8')
        except utils.web.Error:
            irc.reply("LastFM: Unknown ID %s" % uid)
            return

        parser = NewLastFMParser()
        (user, isNowPlaying, artist, track, time) = parser.parseRecentTracks(f)
        if not track:
            irc.reply("No information returned, it's possible user " + uid + " hasn't played anything.")
        else:
            ytquery = track + " by " + artist
            link = self._yt(ytquery)
            if isNowPlaying is True:
                irc.reply(('%s is listening to "%s" by %s | %s'
                        % (user, track, artist, link)).encode("utf8"))
            else:
                if "ago" in time:
                    irc.reply(('%s listened to "%s" by %s %s | %s'
                            % (user, track, artist, time, link)).encode("utf-8"))
                else:
                    irc.reply(('%s listened to "%s" by %s %s | %s'
                            % (user, track, artist,
                                self._formatTimeago(time), link)).encode("utf-8"))

    np = wrap(nowPlaying, [optional("something")])

    def setUserId(self, irc, msg, args, newId):
        """<id>

        Sets the LastFM ID for the caller and saves it in a database.
        """

        self.db.set(msg.nick, newId)

        irc.reply("LastFM ID changed.")
        self.profile(irc, msg, args)

    set = wrap(setUserId, ["something"])

    def profile(self, irc, msg, args, optionalId):
        """[<id>]

        Prints the profile info for the specified LastFM ID.
        Set your LastFM ID with the set method (default is your current nick)
        or specify <id> to switch for one call.
        """

        id = (optionalId or self.db.getId(msg.nick) or msg.nick)

        url = "%s/%s/profile.xml" % (self.APIURL_1_0, id)
        try:
            f = urllib2.urlopen(url)
        except urllib2.HTTPError:
            irc.error("Unknown user (%s)" % id)
            return

        xml = minidom.parse(f).getElementsByTagName("profile")[0]
        keys = "realname registered age gender country playcount".split()
        profile = tuple([self._parse(xml, node) for node in keys])

        irc.reply(("%s (realname: %s) registered on %s; age: %s / %s; \
Country: %s; Tracks played: %s" % ((id,) + profile)).encode("utf8"))

    profile = wrap(profile, [optional("something")])

    def compareUsers(self, irc, msg, args, user1, optionalUser2):
        """user1 [<user2>]

        Compares the taste from two users
        If <user2> is ommitted, the taste is compared against the ID of the calling user.
        """

        user2 = (optionalUser2 or self.db.getId(msg.nick) or msg.nick)

        channel = msg.args[0]
        maxResults = self.registryValue("maxResults", channel)
        # see http://www.lastfm.de/api/show/tasteometer.compare
        url = "%s&method=tasteometer.compare&type1=user&type2=user&value1=%s&value2=%s&limit=%s" % (
            self.APIURL_2_0, user1, user2, maxResults
        )
        try:
            f = urllib2.urlopen(url)
        except urllib2.HTTPError, e:
            irc.error("Failure: %s" % (e))
            return

        xml = minidom.parse(f)
        resultNode = xml.getElementsByTagName("result")[0]
        score = float(self._parse(resultNode, "score"))
        scoreStr = "%s (%s)" % (round(score, 2), self._formatRating(score))
        # Note: XPath would be really cool here...
        artists = [el for el in resultNode.getElementsByTagName("artist")]
        artistNames = [el.getElementsByTagName("name")[0].firstChild.data for el in artists]
        irc.reply(("Result of comparison between %s and %s: score: %s, common artists: %s" \
                % (user1, user2, scoreStr, ", ".join(artistNames))
            ).encode("utf-8")
        )

    compare = wrap(compareUsers, ["something", optional("something")])

    def _parse(self, node, tagName, exceptMsg="not specified"):
            try:
                return node.getElementsByTagName(tagName)[0].firstChild.data
            except IndexError:
                return exceptMsg

    def _formatTimeago(self, unixtime):
        if isinstance(unixtime, basestring):
            year = datetime.now().year
            # Due to last.fm site update, this has to be modified to expect a string at first
            # and then converted, since the data is scraped (see NewLastFMParser).
            
            # check if string already contains year and insert it if it doesn't, if track was 
            # played during the current year it will not include it.
            if len(unixtime) <= 14:
                splitstring = unixtime.rpartition(':')
                # string returned from LastFM does not include leading zeroes in date
                # for example jan 1 instead of jan 01
                if splitstring[0][-2].isdigit() == True:
                    unixtime = splitstring[0][:len(splitstring[0])-2] + "%i, " % year + \
                        splitstring[0][-2:] + splitstring[1] + splitstring[2]
                else:
                    unixtime = splitstring[0][:len(splitstring[0])-1] + "%i, " % year + \
                        splitstring[0][-1:] + splitstring[1] + splitstring[2]
            
            # convert string to timestamp
            unixtime = mktime(datetime.strptime(unixtime, "%d %b %Y, %I:%M%p").timetuple())
            
        t = int(time()-unixtime)
        if t/86400 > 0:
            return "%i days ago" % (t/86400)
        if t/3600 > 0:
            return "%i hours ago" % (t/3600)
        if t/60 > 0:
            return "%i minutes ago" % (t/60)
        if t > 0:
            return "%i seconds ago" % (t)

    def _formatRating(self, score):
        """Format rating

        @param score Value in the form of [0:1] (float)
        """

        if score >= 0.9:
            return "Super"
        elif score >= 0.7:
            return "Very High"
        elif score >= 0.5:
            return "High"
        elif score >= 0.3:
            return "Medium"
        elif score >= 0.1:
            return "Low"
        else:
            return "Very Low"

dbfilename = conf.supybot.directories.data.dirize("LastFM.db")

Class = LastFM


# vim:set shiftwidth=4 tabstop=4 expandtab textwidth=79:
