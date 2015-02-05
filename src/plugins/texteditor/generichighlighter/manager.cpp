/****************************************************************************
**
** Copyright (C) 2015 The Qt Company Ltd.
** Contact: http://www.qt.io/licensing
**
** This file is part of Qt Creator.
**
** Commercial License Usage
** Licensees holding valid commercial Qt licenses may use this file in
** accordance with the commercial license agreement provided with the
** Software or, alternatively, in accordance with the terms contained in
** a written agreement between you and The Qt Company.  For licensing terms and
** conditions see http://www.qt.io/terms-conditions.  For further information
** use the contact form at http://www.qt.io/contact-us.
**
** GNU Lesser General Public License Usage
** Alternatively, this file may be used under the terms of the GNU Lesser
** General Public License version 2.1 or version 3 as published by the Free
** Software Foundation and appearing in the file LICENSE.LGPLv21 and
** LICENSE.LGPLv3 included in the packaging of this file.  Please review the
** following information to ensure the GNU Lesser General Public License
** requirements will be met: https://www.gnu.org/licenses/lgpl.html and
** http://www.gnu.org/licenses/old-licenses/lgpl-2.1.html.
**
** In addition, as a special exception, The Qt Company gives you certain additional
** rights.  These rights are described in The Qt Company LGPL Exception
** version 1.1, included in the file LGPL_EXCEPTION.txt in this package.
**
****************************************************************************/

#include "manager.h"
#include "highlightdefinition.h"
#include "highlightdefinitionhandler.h"
#include "highlighterexception.h"
#include "definitiondownloader.h"
#include "highlightersettings.h"
#include <texteditor/plaintexteditorfactory.h>
#include <texteditor/texteditorconstants.h>
#include <texteditor/texteditorsettings.h>

#include <coreplugin/icore.h>
#include <coreplugin/messagemanager.h>
#include <coreplugin/progressmanager/progressmanager.h>
#include <utils/algorithm.h>
#include <utils/QtConcurrentTools>
#include <utils/networkaccessmanager.h>

#include <QCoreApplication>
#include <QString>
#include <QStringList>
#include <QFile>
#include <QFileInfo>
#include <QDir>
#include <QRegExp>
#include <QFuture>
#include <QtConcurrentMap>
#include <QUrl>
#include <QSet>
#include <QXmlStreamReader>
#include <QXmlStreamAttributes>
#include <QMessageBox>
#include <QXmlSimpleReader>
#include <QXmlInputSource>
#include <QNetworkRequest>
#include <QNetworkReply>

using namespace Core;

namespace TextEditor {
namespace Internal {

const char kPriority[] = "priority";
const char kName[] = "name";
const char kExtensions[] = "extensions";
const char kMimeType[] = "mimetype";
const char kVersion[] = "version";
const char kUrl[] = "url";

class MultiDefinitionDownloader : public QObject
{
    Q_OBJECT

public:
    MultiDefinitionDownloader(const QString &savePath, const QList<QString> &installedDefinitions) :
        m_installedDefinitions(installedDefinitions),
        m_downloadPath(savePath)
    {
        connect(&m_downloadWatcher, SIGNAL(finished()), this, SLOT(downloadDefinitionsFinished()));
    }

    ~MultiDefinitionDownloader()
    {
        if (m_downloadWatcher.isRunning())
            m_downloadWatcher.cancel();
    }

    void downloadDefinitions(const QList<QUrl> &urls);

signals:
    void finished();

private slots:
    void downloadReferencedDefinition(const QString &name);
    void downloadDefinitionsFinished();

private:
    QFutureWatcher<void> m_downloadWatcher;
    QList<DefinitionDownloader *> m_downloaders;
    QList<QString> m_installedDefinitions;
    QSet<QString> m_referencedDefinitions;
    QString m_downloadPath;
};

Manager::Manager() :
    m_multiDownloader(0),
    m_hasQueuedRegistration(false)
{
    connect(&m_registeringWatcher, SIGNAL(finished()), this, SLOT(registerMimeTypesFinished()));
}

Manager::~Manager()
{
    disconnect(&m_registeringWatcher);
    disconnect(m_multiDownloader);
    if (m_registeringWatcher.isRunning())
        m_registeringWatcher.cancel();
    delete m_multiDownloader;
}

Manager *Manager::instance()
{
    static Manager manager;
    return &manager;
}

QString Manager::definitionIdByName(const QString &name) const
{
    return m_register.m_idByName.value(name);
}

QString Manager::definitionIdByMimeType(const QString &mimeType) const
{
    return m_register.m_idByMimeType.value(mimeType);
}

QString Manager::definitionIdByAnyMimeType(const QStringList &mimeTypes) const
{
    QString definitionId;
    foreach (const QString &mimeType, mimeTypes) {
        definitionId = definitionIdByMimeType(mimeType);
        if (!definitionId.isEmpty())
            break;
    }
    return definitionId;
}

DefinitionMetaDataPtr Manager::availableDefinitionByName(const QString &name) const
{
    return m_availableDefinitions.value(name);
}

QSharedPointer<HighlightDefinition> Manager::definition(const QString &id)
{
    if (!id.isEmpty() && !m_definitions.contains(id)) {
        QFile definitionFile(id);
        if (!definitionFile.open(QIODevice::ReadOnly | QIODevice::Text))
            return QSharedPointer<HighlightDefinition>();

        QSharedPointer<HighlightDefinition> definition(new HighlightDefinition);
        HighlightDefinitionHandler handler(definition);

        QXmlInputSource source(&definitionFile);
        QXmlSimpleReader reader;
        reader.setContentHandler(&handler);
        m_isBuildingDefinition.insert(id);
        try {
            reader.parse(source);
        } catch (const HighlighterException &e) {
            MessageManager::write(
                        QCoreApplication::translate("GenericHighlighter",
                                                    "Generic highlighter error: ") + e.message(),
                        MessageManager::WithFocus);
            definition.clear();
        }
        m_isBuildingDefinition.remove(id);
        definitionFile.close();

        m_definitions.insert(id, definition);
    }

    return m_definitions.value(id);
}

DefinitionMetaDataPtr Manager::definitionMetaData(const QString &id) const
{
    return m_register.m_definitionsMetaData.value(id);
}

bool Manager::isBuildingDefinition(const QString &id) const
{
    return m_isBuildingDefinition.contains(id);
}

class ManagerProcessor : public QObject
{
    Q_OBJECT
public:
    ManagerProcessor();
    void process(QFutureInterface<QPair<Manager::RegisterData,
                                        QList<MimeType> > > &future);

    QStringList m_definitionsPaths;
    QSet<QString> m_knownMimeTypes;
    QSet<QString> m_knownSuffixes;
    QHash<QString, MimeType> m_userModified;
    static const int kMaxProgress;
};

const int ManagerProcessor::kMaxProgress = 200;

ManagerProcessor::ManagerProcessor()
    : m_knownSuffixes(QSet<QString>::fromList(MimeDatabase::suffixes()))
{
    const HighlighterSettings &settings = TextEditorSettings::highlighterSettings();
    m_definitionsPaths.append(settings.definitionFilesPath());
    if (settings.useFallbackLocation())
        m_definitionsPaths.append(settings.fallbackDefinitionFilesPath());

    foreach (const MimeType &userMimeType, MimeDatabase::readUserModifiedMimeTypes())
        m_userModified.insert(userMimeType.type(), userMimeType);
    foreach (const MimeType &mimeType, MimeDatabase::mimeTypes())
        m_knownMimeTypes.insert(mimeType.type());
}

void ManagerProcessor::process(QFutureInterface<QPair<Manager::RegisterData,
                                                      QList<MimeType> > > &future)
{
    future.setProgressRange(0, kMaxProgress);

    // @TODO: Improve MIME database to handle the following limitation.
    // The generic highlighter only register its types after all other plugins
    // have populated Creator's MIME database (so it does not override anything).
    // When the generic highlighter settings change only its internal data is cleaned-up
    // and rebuilt. Creator's MIME database is not touched. So depending on how the
    // user plays around with the generic highlighter file definitions (changing
    // duplicated patterns, for example), some changes might not be reflected.
    // A definitive implementation would require some kind of re-load or update
    // (considering hierarchies, aliases, etc) of the MIME database whenever there
    // is a change in the generic highlighter settings.

    Manager::RegisterData data;
    QList<MimeType> newMimeTypes;

    foreach (const QString &path, m_definitionsPaths) {
        if (path.isEmpty())
            continue;

        QDir definitionsDir(path);
        QStringList filter(QLatin1String("*.xml"));
        definitionsDir.setNameFilters(filter);
        QList<DefinitionMetaDataPtr> allMetaData;
        foreach (const QFileInfo &fileInfo, definitionsDir.entryInfoList()) {
            const DefinitionMetaDataPtr &metaData =
                    Manager::parseMetadata(fileInfo);
            if (!metaData.isNull())
                allMetaData.append(metaData);
        }

        // Consider definitions with higher priority first.
        Utils::sort(allMetaData, [](const DefinitionMetaDataPtr &l,
                                    const DefinitionMetaDataPtr &r) {
            return l->priority > r->priority;
        });

        foreach (const DefinitionMetaDataPtr &metaData, allMetaData) {
            if (future.isCanceled())
                return;
            if (future.progressValue() < kMaxProgress - 1)
                future.setProgressValue(future.progressValue() + 1);

            if (data.m_idByName.contains(metaData->name))
                // Name already exists... This is a fallback item, do not consider it.
                continue;

            const QString &id = metaData->id;
            data.m_idByName.insert(metaData->name, id);
            data.m_definitionsMetaData.insert(id, metaData);

            static const QStringList textPlain(QLatin1String("text/plain"));

            // A definition can specify multiple MIME types and file extensions/patterns,
            // but all on a single string. So associate all patterns with all MIME types.
            QList<MimeGlobPattern> globPatterns;
            foreach (const QString &type, metaData->mimeTypes) {
                if (data.m_idByMimeType.contains(type))
                    continue;

                data.m_idByMimeType.insert(type, id);
                if (!m_knownMimeTypes.contains(type)) {
                    m_knownMimeTypes.insert(type);

                    MimeType mimeType;
                    mimeType.setType(type);
                    mimeType.setSubClassesOf(textPlain);
                    mimeType.setComment(metaData->name);

                    // If there's a user modification for this mime type, we want to use the
                    // modified patterns and rule-based matchers. If not, just consider what
                    // is specified in the definition file.
                    QHash<QString, MimeType>::const_iterator it =
                        m_userModified.find(mimeType.type());
                    if (it == m_userModified.end()) {
                        if (globPatterns.isEmpty()) {
                            foreach (const QString &pattern, metaData->patterns) {
                                static const QLatin1String mark("*.");
                                if (pattern.startsWith(mark)) {
                                    const QString &suffix = pattern.right(pattern.length() - 2);
                                    if (!m_knownSuffixes.contains(suffix))
                                        m_knownSuffixes.insert(suffix);
                                    else
                                        continue;
                                }
                                globPatterns.append(MimeGlobPattern(pattern, 50));
                            }
                        }
                        mimeType.setGlobPatterns(globPatterns);
                    } else {
                        mimeType.setGlobPatterns(it.value().globPatterns());
                        mimeType.setMagicRuleMatchers(it.value().magicRuleMatchers());
                    }

                    newMimeTypes.append(mimeType);
                }
            }
        }
    }

    future.reportResult(qMakePair(data, newMimeTypes));
}

void Manager::registerMimeTypes()
{
    if (!m_registeringWatcher.isRunning()) {
        clear();

        ManagerProcessor *processor = new ManagerProcessor;
        QFuture<QPair<RegisterData, QList<MimeType> > > future =
            QtConcurrent::run(&ManagerProcessor::process, processor);
        connect(&m_registeringWatcher, SIGNAL(finished()), processor, SLOT(deleteLater()));
        m_registeringWatcher.setFuture(future);
    } else {
        m_hasQueuedRegistration = true;
        m_registeringWatcher.cancel();
    }
}

void Manager::registerMimeTypesFinished()
{
    if (m_hasQueuedRegistration) {
        m_hasQueuedRegistration = false;
        registerMimeTypes();
    } else if (!m_registeringWatcher.isCanceled()) {
        const QPair<RegisterData, QList<MimeType> > &result = m_registeringWatcher.result();
        m_register = result.first;

        foreach (const MimeType &mimeType, result.second)
            MimeDatabase::addMimeType(mimeType);

        emit mimeTypesRegistered();
    }
}

DefinitionMetaDataPtr Manager::parseMetadata(const QFileInfo &fileInfo)
{
    static const QLatin1Char kSemiColon(';');
    static const QLatin1Char kSpace(' ');
    static const QLatin1Char kDash('-');
    static const QLatin1String kLanguage("language");
    static const QLatin1String kArtificial("text/x-artificial-");

    QFile definitionFile(fileInfo.absoluteFilePath());
    if (!definitionFile.open(QIODevice::ReadOnly | QIODevice::Text))
        return DefinitionMetaDataPtr();

    DefinitionMetaDataPtr metaData(new HighlightDefinitionMetaData);

    QXmlStreamReader reader(&definitionFile);
    while (!reader.atEnd() && !reader.hasError()) {
        if (reader.readNext() == QXmlStreamReader::StartElement && reader.name() == kLanguage) {
            const QXmlStreamAttributes &atts = reader.attributes();

            metaData->fileName = fileInfo.fileName();
            metaData->id = fileInfo.absoluteFilePath();
            metaData->name = atts.value(QLatin1String(kName)).toString();
            metaData->version = atts.value(QLatin1String(kVersion)).toString();
            metaData->priority = atts.value(QLatin1String(kPriority)).toString().toInt();
            metaData->patterns = atts.value(QLatin1String(kExtensions))
                                  .toString().split(kSemiColon, QString::SkipEmptyParts);

            QStringList mimeTypes = atts.value(QLatin1String(kMimeType)).
                                    toString().split(kSemiColon, QString::SkipEmptyParts);
            if (mimeTypes.isEmpty()) {
                // There are definitions which do not specify a MIME type, but specify file
                // patterns. Creating an artificial MIME type is a workaround.
                QString artificialType(kArtificial);
                artificialType.append(metaData->name.trimmed().replace(kSpace, kDash));
                mimeTypes.append(artificialType);
            }
            metaData->mimeTypes = mimeTypes;

            break;
        }
    }
    reader.clear();
    definitionFile.close();

    return metaData;
}

QList<DefinitionMetaDataPtr> Manager::parseAvailableDefinitionsList(QIODevice *device)
{
    static const QLatin1Char kSlash('/');
    static const QLatin1String kDefinition("Definition");

    m_availableDefinitions.clear();
    QXmlStreamReader reader(device);
    while (!reader.atEnd() && !reader.hasError()) {
        if (reader.readNext() == QXmlStreamReader::StartElement &&
            reader.name() == kDefinition) {
            const QXmlStreamAttributes &atts = reader.attributes();

            DefinitionMetaDataPtr metaData(new HighlightDefinitionMetaData);
            metaData->name = atts.value(QLatin1String(kName)).toString();
            metaData->version = atts.value(QLatin1String(kVersion)).toString();
            QString url = atts.value(QLatin1String(kUrl)).toString();
            metaData->url = QUrl(url);
            const int slash = url.lastIndexOf(kSlash);
            if (slash != -1)
                metaData->fileName = url.right(url.length() - slash - 1);

            m_availableDefinitions.insert(metaData->name, metaData);
        }
    }
    reader.clear();

    return m_availableDefinitions.values();
}

void Manager::downloadAvailableDefinitionsMetaData()
{
    QUrl url(QLatin1String("http://www.kate-editor.org/syntax/update-3.9.xml"));
    QNetworkRequest request(url);
    // Currently this takes a couple of seconds on Windows 7: QTBUG-10106.
    QNetworkReply *reply = Utils::NetworkAccessManager::instance()->get(request);
    connect(reply, SIGNAL(finished()), this, SLOT(downloadAvailableDefinitionsListFinished()));
}

void Manager::downloadAvailableDefinitionsListFinished()
{
    if (QNetworkReply *reply = qobject_cast<QNetworkReply *>(sender())) {
        if (reply->error() == QNetworkReply::NoError)
            emit definitionsMetaDataReady(parseAvailableDefinitionsList(reply));
        else
            emit errorDownloadingDefinitionsMetaData();
        reply->deleteLater();
    }
}

void Manager::downloadDefinitions(const QList<QUrl> &urls, const QString &savePath)
{
    m_multiDownloader = new MultiDefinitionDownloader(savePath, m_register.m_idByName.keys());
    connect(m_multiDownloader, SIGNAL(finished()), this, SLOT(downloadDefinitionsFinished()));
    m_multiDownloader->downloadDefinitions(urls);
}

void MultiDefinitionDownloader::downloadDefinitions(const QList<QUrl> &urls)
{
    m_downloaders.clear();
    foreach (const QUrl &url, urls) {
        DefinitionDownloader *downloader = new DefinitionDownloader(url, m_downloadPath);
        connect(downloader, SIGNAL(foundReferencedDefinition(QString)),
                this, SLOT(downloadReferencedDefinition(QString)));
        m_downloaders.append(downloader);
    }

    QFuture<void> future = QtConcurrent::map(m_downloaders, DownloaderStarter());
    m_downloadWatcher.setFuture(future);
    ProgressManager::addTask(future, tr("Downloading Highlighting Definitions"),
                             "TextEditor.Task.Download");
}

void MultiDefinitionDownloader::downloadDefinitionsFinished()
{
    int errors = 0;
    bool writeError = false;
    foreach (DefinitionDownloader *downloader, m_downloaders) {
        DefinitionDownloader::Status status = downloader->status();
        if (status != DefinitionDownloader::Ok) {
            ++errors;
            if (status == DefinitionDownloader::WriteError && !writeError)
                writeError = true;
        }
        delete downloader;
    }

    if (errors > 0) {
        QString text;
        if (errors == m_downloaders.size())
            text = tr("Error downloading selected definition(s).");
        else
            text = tr("Error downloading one or more definitions.");
        if (writeError)
            text.append(QLatin1Char('\n') + tr("Please check the directory's access rights."));
        QMessageBox::critical(0, tr("Download Error"), text);
    }

    QList<QUrl> urls;
    foreach (const QString &definition, m_referencedDefinitions) {
        if (DefinitionMetaDataPtr metaData =
                Manager::instance()->availableDefinitionByName(definition)) {
            urls << metaData->url;
        }
    }
    m_referencedDefinitions.clear();
    if (urls.isEmpty())
        emit finished();
    else
        downloadDefinitions(urls);
}

void Manager::downloadDefinitionsFinished()
{
    delete m_multiDownloader;
    m_multiDownloader = 0;
}

void MultiDefinitionDownloader::downloadReferencedDefinition(const QString &name)
{
    if (m_installedDefinitions.contains(name))
        return;
    m_referencedDefinitions.insert(name);
    m_installedDefinitions.append(name);
}

bool Manager::isDownloadingDefinitions() const
{
    return m_multiDownloader != 0;
}

void Manager::clear()
{
    m_register.m_idByName.clear();
    m_register.m_idByMimeType.clear();
    m_register.m_definitionsMetaData.clear();
    m_definitions.clear();
}

} // namespace Internal
} // namespace TextEditor

#include "manager.moc"