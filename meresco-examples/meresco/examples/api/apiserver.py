#-*- coding: utf-8 -*-
## begin license ##
#
# Drents Archief beoogt het Drents erfgoed centraal beschikbaar te stellen.
#
# Copyright (C) 2012-2016 Seecr (Seek You Too B.V.) http://seecr.nl
# Copyright (C) 2012-2014 Stichting Bibliotheek.nl (BNL) http://www.bibliotheek.nl
# Copyright (C) 2015-2016 Drents Archief http://www.drentsarchief.nl
# Copyright (C) 2015 Koninklijke Bibliotheek (KB) http://www.kb.nl
#
# This file is part of "Drents Archief"
#
# "Drents Archief" is free software; you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation; either version 2 of the License, or
# (at your option) any later version.
#
# "Drents Archief" is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with "Drents Archief"; if not, write to the Free Software
# Foundation, Inc., 51 Franklin St, Fifth Floor, Boston, MA  02110-1301  USA
#
## end license ##

import sys
from os.path import join, dirname, abspath

from weightless.core import be, consume
from weightless.io import Reactor

from meresco.components.drilldown import SruTermDrilldown
from meresco.core import Observable
from meresco.core.alltodo import AllToDo
from meresco.core.processtools import setSignalHandlers, registerShutdownHandler

from meresco.components import RenameFieldForExact, PeriodicDownload, XmlPrintLxml, XmlXPath, FilterMessages, RewritePartname, XmlParseLxml, CqlMultiSearchClauseConversion, PeriodicCall, Schedule, Rss, RssItem
from meresco.components.cql import SearchTermFilterAndModifier
from meresco.components.http import ObservableHttpServer, BasicHttpHandler, PathFilter, Deproxy
from meresco.components.log import LogCollector, ApacheLogWriter, HandleRequestLog, LogCollectorScope, QueryLogWriter, DirectoryLog, LogFileServer, LogComponent
from meresco.components.sru import SruHandler, SruParser, SruLimitStartRecord

from meresco.oai import OaiDownloadProcessor, UpdateAdapterFromOaiDownloadProcessor, OaiJazz, OaiPmh, OaiAddDeleteRecordWithPrefixesAndSetSpecs, OaiBranding, OaiProvenance


from meresco.lucene import SORTED_PREFIX, UNTOKENIZED_PREFIX
from meresco.lucene.remote import LuceneRemote
from meresco.lucene.converttocomposedquery import ConvertToComposedQuery

from seecr.utils import DebugPrompt

from meresco.components.drilldownqueries import DrilldownQueries
from storage import StorageComponent
from meresco.dans.storagesplit import Md5HashDistributeStrategy

from storage.storageadapter import StorageAdapter

from meresco.examples.index.indexserver import untokenizedFieldname, untokenizedFieldnames, DEFAULT_CORE
from meresco.examples.gateway.gatewayserver import DEFAULT_PARTNAME


def createDownloadHelix(reactor, periodicDownload, oaiDownload, storageComponent, oaiJazz):
    return \
    (periodicDownload, # Scheduled connection to a remote (response / request)...
        (XmlParseLxml(fromKwarg="data", toKwarg="lxmlNode", parseOptions=dict(huge_tree=True, remove_blank_text=True)), # Convert from plain text to lxml-object.
            (oaiDownload, # Implementation/Protocol of a PeriodicDownload...
                (UpdateAdapterFromOaiDownloadProcessor(), # Maakt van een SRU update/delete bericht (lxmlNode) een relevante message: 'delete' of 'add' message.
                    (RewritePartname(DEFAULT_PARTNAME), # Hernoemt partname van 'record' naar DEFAULT_PARTNAME.
                        (FilterMessages(['delete']), # Filtert delete messages
                            (storageComponent,), # Delete from storage
                            (oaiJazz,), # Delete from OAI-pmh repo
                        ),
                        (FilterMessages(['add']),
                            (XmlXPath(['/oai:record/oai:metadata/document:document/document:part[@name="record"]/text()'], fromKwarg='lxmlNode', toKwarg='data'),
                                (XmlParseLxml(fromKwarg='data', toKwarg='lxmlNode'),
                                    (XmlXPath(['/oai:record/oai:metadata/oai_dc:dc'], fromKwarg='lxmlNode'),
                                        (XmlPrintLxml(fromKwarg="lxmlNode", toKwarg="data", pretty_print=False),
                                            (storageComponent,)
                                        ), #metadataPrefixes=None, setSpecs=None, name=None
                                        (OaiAddDeleteRecordWithPrefixesAndSetSpecs(metadataPrefixes=['oai_dc'], setSpecs=['publications'], name='NARCISPORTAL'),
                                            (oaiJazz,),
                                        )
                                    )
                                )
                            )
                        )
                    )
                )
            )
        )
    )

def main(reactor, port, statePath, indexPort, gatewayPort, **ignored):
    apacheLogStream = sys.stdout

    periodicGateWayDownload = PeriodicDownload(
        reactor,
        host='localhost',
        port=gatewayPort,
        name='gateway')

    oaiDownload = OaiDownloadProcessor(
        path='/oaix',
        metadataPrefix=DEFAULT_PARTNAME,
        workingDirectory=join(statePath, 'harvesterstate', 'gateway'),
        xWait=True,
        name='gateway',
        autoCommit=False)

    # def sortFieldRename(name):
    #     if not name.startswith('__'):
    #         name = SORTED_PREFIX + name
    #     return name

    fieldnameRewrites = {
        UNTOKENIZED_PREFIX+'genre': UNTOKENIZED_PREFIX+'dc:genre',
    }

    def fieldnameRewrite(name):
        return fieldnameRewrites.get(name, name)

    def drilldownFieldnamesTranslate(fieldname):
        untokenizedName = untokenizedFieldname(fieldname)
        if untokenizedName in untokenizedFieldnames:
            fieldname = untokenizedName
        return fieldnameRewrite(fieldname)

    convertToComposedQuery = ConvertToComposedQuery(
            resultsFrom=DEFAULT_CORE,
            matches=[],
            drilldownFieldnamesTranslate=drilldownFieldnamesTranslate
        )

    luceneRemote = LuceneRemote(host='localhost', port=indexPort, path='/lucene')

    strategie = Md5HashDistributeStrategy()
    storage = StorageComponent(join(statePath, 'store'), strategy=strategie, partsRemovedOnDelete=[DEFAULT_PARTNAME])

    oaiJazz = OaiJazz(join(statePath, 'oai'))
    oaiJazz.updateMetadataFormat(DEFAULT_PARTNAME, None, None) # def updateMetadataFormat(self, prefix, schema, namespace):

    # Wat doet dit?
    cqlClauseConverters = [
        RenameFieldForExact(
            untokenizedFields=untokenizedFieldnames,
            untokenizedPrefix=UNTOKENIZED_PREFIX,
        ).filterAndModifier(),
        SearchTermFilterAndModifier(
            shouldModifyFieldValue=lambda *args: True,
            fieldnameModifier=fieldnameRewrite
        ).filterAndModifier(),
    ]

    # # Post commit naar storage en ??
    # scheduledCommitPeriodicCall = be(
    #     (PeriodicCall(reactor, message='commit', name='Scheduled commit', initialSchedule=Schedule(period=1), schedule=Schedule(period=1)),
    #         (AllToDo(),
    #             (LogComponent("PeriodicCall"),), # commit(*(), **{})
    #             (storage,),
    #             (periodicGateWayDownload,),
    #         )
    #     )
    # )

    directoryLog = DirectoryLog(join(statePath, 'log'), extension='-query.log')

    executeQueryHelix = \
        (FilterMessages(allowed=['executeQuery']),
            (CqlMultiSearchClauseConversion(cqlClauseConverters, fromKwarg='query'),
                (DrilldownQueries(),
                    (convertToComposedQuery,
                        (luceneRemote,),
                    )
                )
            )
        )

    return \
    (Observable(),
        # (scheduledCommitPeriodicCall,),
        # (DebugPrompt(reactor=reactor, port=port+1, globals=locals()),),
        createDownloadHelix(reactor, periodicGateWayDownload, oaiDownload, storage, oaiJazz),
        (ObservableHttpServer(reactor, port, compressResponse=True),
            (LogCollector(),
                (ApacheLogWriter(apacheLogStream),),
                (QueryLogWriter.forHttpArguments(
                        log=directoryLog,
                        scopeNames=('http-scope',)
                    ),
                ),
                (QueryLogWriter(log=directoryLog, scopeNames=('sru-scope',)),),
                (Deproxy(),
                    (HandleRequestLog(),
                        (BasicHttpHandler(),
                            (PathFilter(["/oai"]),
                                (LogCollectorScope("http-scope"),
                                    (OaiPmh(repositoryName="NARCIS OAI-pmh", adminEmail="narcis@dans.knaw.nl"),
                                        (oaiJazz,),
                                        (StorageAdapter(),
                                            (storage,)
                                        ),
                                        (OaiBranding(url="http://www.narcis.nl/images/logos/logo-knaw-house.gif", link="http://oai.narcis.nl", title="Narcis - The gateway to scholarly information in The Netherlands"),),
                                        # (OaiProvenance(nsMap={}, baseURL='http://dds.nl', harvestDate='2016-02-02', metadataNamespace='urn:didl', identifier='unique', datestamp='2016-01-01'),),
                                    )
                                )
                            ),
                            (PathFilter(['/sru']),
                                (LogCollectorScope('sru-scope'),
                                    (SruParser(
                                            host='example.org',
                                            port=80,
                                            defaultRecordSchema=DEFAULT_PARTNAME,
                                            defaultRecordPacking='xml'),
                                        (SruLimitStartRecord(limitBeyond=4000),
                                            (SruHandler(
                                                    includeQueryTimes=False,
                                                    extraXParameters=[],
                                                    enableCollectLog=True),
                                                (SruTermDrilldown(),),
                                                executeQueryHelix,
                                                (StorageAdapter(),
                                                    (storage,)
                                                )
                                            )
                                        )
                                    )
                                )
                            ),
                            (PathFilter('/rss'),
                                (Rss(   title = 'Meresco',
                                        description = 'RSS feed for Meresco',
                                        link = 'http://meresco.org',
                                        maximumRecords = 20),
                                    executeQueryHelix,
                                    (RssItem(
                                            nsMap={
                                                'dc': "http://purl.org/dc/elements/1.1/",
                                                'oai_dc': "http://www.openarchives.org/OAI/2.0/oai_dc/"
                                            },
                                            title = ('oai_dc', '/oai_dc:dc/dc:title/text()'),
                                            description = ('oai_dc', '/oai_dc:dc/dc:description/text()'),
                                            linkTemplate = 'http://localhost/sru?operation=searchRetrieve&version=1.2&query=dc:identifier%%3D%(identifier)s',
                                            identifier = ('oai_dc', '/oai_dc:dc/dc:identifier/text()')),
                                        (StorageAdapter(),
                                            (storage,)
                                        )
                                    )
                                )
                            ),
                            (PathFilter('/log'),
                                (LogFileServer(name="Example Queries", log=directoryLog, basepath='/log'),)
                            )
                        )
                    )
                )
            )
        )
    )

def startServer(port, stateDir, **kwargs):
    setSignalHandlers()
    print 'Firing up API Server.'
    reactor = Reactor()
    statePath = abspath(stateDir)

    #main
    dna = main(
        reactor=reactor,
        port=port,
        statePath=statePath,
        **kwargs
    )
    #/main

    server = be(dna)
    consume(server.once.observer_init())

    registerShutdownHandler(statePath=statePath, server=server, reactor=reactor, shutdownMustSucceed=False)

    print "Ready to rumble at %s" % port
    sys.stdout.flush()
    reactor.loop()
