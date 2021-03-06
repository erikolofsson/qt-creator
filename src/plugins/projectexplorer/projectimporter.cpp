/****************************************************************************
**
** Copyright (C) 2016 The Qt Company Ltd.
** Contact: https://www.qt.io/licensing/
**
** This file is part of Qt Creator.
**
** Commercial License Usage
** Licensees holding valid commercial Qt licenses may use this file in
** accordance with the commercial license agreement provided with the
** Software or, alternatively, in accordance with the terms contained in
** a written agreement between you and The Qt Company. For licensing terms
** and conditions see https://www.qt.io/terms-conditions. For further
** information use the contact form at https://www.qt.io/contact-us.
**
** GNU General Public License Usage
** Alternatively, this file may be used under the terms of the GNU
** General Public License version 3 as published by the Free Software
** Foundation with exceptions as appearing in the file LICENSE.GPL3-EXCEPT
** included in the packaging of this file. Please review the following
** information to ensure the GNU General Public License requirements will
** be met: https://www.gnu.org/licenses/gpl-3.0.html.
**
****************************************************************************/

#include "projectimporter.h"

#include "buildinfo.h"
#include "kit.h"
#include "kitinformation.h"
#include "kitmanager.h"
#include "project.h"
#include "projectexplorerconstants.h"
#include "target.h"

#include <coreplugin/icore.h>

#include <utils/algorithm.h>
#include <utils/qtcassert.h>

#include <QLoggingCategory>
#include <QMessageBox>
#include <QString>

namespace ProjectExplorer {

static const Core::Id KIT_IS_TEMPORARY("PE.TempKit");
static const Core::Id KIT_TEMPORARY_NAME("PE.TempName");
static const Core::Id KIT_FINAL_NAME("PE.FinalName");
static const Core::Id TEMPORARY_OF_PROJECTS("PE.TempProject");

static Core::Id fullId(Core::Id id)
{
    const QString prefix = "PE.Temporary.";

    const QString idStr = id.toString();
    QTC_ASSERT(!idStr.startsWith(prefix), return Core::Id::fromString(idStr));

    return Core::Id::fromString(prefix + idStr);
}

static bool hasOtherUsers(Core::Id id, const QVariant &v, Kit *k)
{
    return Utils::contains(KitManager::kits(), [id, v, k](Kit *in) -> bool {
        if (in == k)
            return false;
        QVariantList tmp = in->value(id).toList();
        return tmp.contains(v);
    });
}

ProjectImporter::ProjectImporter(const QString &path) : m_projectPath(path)
{ }

ProjectImporter::~ProjectImporter()
{
    foreach (Kit *k, KitManager::kits())
        removeProject(k);
}

QList<BuildInfo *> ProjectImporter::import(const Utils::FileName &importPath, bool silent)
{
    QList<BuildInfo *> result;

    const QLoggingCategory log("qtc.projectexplorer.import");
    qCDebug(log) << "ProjectImporter::import" << importPath << silent;

    QFileInfo fi = importPath.toFileInfo();
    if (!fi.exists() && !fi.isDir()) {
        qCDebug(log) << "**doesn't exist";
        return result;
    }

    const Utils::FileName absoluteImportPath = Utils::FileName::fromString(fi.absoluteFilePath());

    qCDebug(log) << "Examining directory" << absoluteImportPath.toString();
    QList<void *> dataList = examineDirectory(absoluteImportPath);
    if (dataList.isEmpty()) {
        qCDebug(log) << "Nothing to import found in" << absoluteImportPath.toString();
        return result;
    }

    qCDebug(log) << "Looking for kits";
    foreach (void *data, dataList) {
        QTC_ASSERT(data, continue);
        QList<Kit *> kitList;
        const QList<Kit *> tmp
                = Utils::filtered(KitManager::kits(), [this, data](Kit *k) { return matchKit(data, k); });
        if (tmp.isEmpty()) {
            kitList += createKit(data);
            qCDebug(log) << "  no matching kit found, temporary kit created.";
        } else {
            kitList += tmp;
            qCDebug(log) << "  " << tmp.count() << "matching kits found.";
        }

        foreach (Kit *k, kitList) {
            qCDebug(log) << "Creating buildinfos for kit" << k->displayName();
            QList<BuildInfo *> infoList = buildInfoListForKit(k, data);
            if (infoList.isEmpty()) {
                qCDebug(log) << "No build infos for kit" << k->displayName();
                continue;
            }

            addProject(k);

            foreach (BuildInfo *i, infoList) {
                if (!Utils::contains(result, [i](const BuildInfo *o) { return (*i) == (*o); }))
                    result += i;
            }
        }
    }

    foreach (auto *dd, dataList)
        deleteDirectoryData(dd);
    dataList.clear();

    if (result.isEmpty() && !silent)
        QMessageBox::critical(Core::ICore::mainWindow(),
                              QCoreApplication::translate("ProjectExplorer::ProjectImporter", "No Build Found"),
                              QCoreApplication::translate("ProjectExplorer::ProjectImporter", "No build found in %1 matching project %2.")
                .arg(importPath.toUserOutput()).arg(QDir::toNativeSeparators(projectFilePath())));

    return result;
}

Target *ProjectImporter::preferredTarget(const QList<Target *> &possibleTargets)
{
    // Select active target
    // a) The default target
    // c) Desktop target
    // d) the first target
    Target *activeTarget = nullptr;
    if (possibleTargets.isEmpty())
        return activeTarget;

    activeTarget = possibleTargets.at(0);
    bool pickedFallback = false;
    foreach (Target *t, possibleTargets) {
        if (t->kit() == KitManager::defaultKit())
            return t;
        if (pickedFallback)
            continue;
        if (DeviceTypeKitInformation::deviceTypeId(t->kit()) == Constants::DESKTOP_DEVICE_TYPE) {
            activeTarget = t;
            pickedFallback = true;
        }
    }
    return activeTarget;
}

void ProjectImporter::markKitAsTemporary(Kit *k) const
{
    QTC_ASSERT(!k->hasValue(KIT_IS_TEMPORARY), return);

    UpdateGuard guard(*this);

    const QString name = k->displayName();
    k->setUnexpandedDisplayName(QCoreApplication::translate("ProjectExplorer::ProjectImporter",
                                                  "%1 - temporary").arg(name));

    k->setValue(KIT_TEMPORARY_NAME, k->displayName());
    k->setValue(KIT_FINAL_NAME, name);
    k->setValue(KIT_IS_TEMPORARY, true);
}

void ProjectImporter::makePersistent(Kit *k) const
{
    if (!k->hasValue(KIT_IS_TEMPORARY))
        return;

    UpdateGuard guard(*this);

    KitGuard kitGuard(k);
    k->removeKey(KIT_IS_TEMPORARY);
    k->removeKey(TEMPORARY_OF_PROJECTS);
    const QString tempName = k->value(KIT_TEMPORARY_NAME).toString();
    if (!tempName.isNull() && k->displayName() == tempName)
        k->setUnexpandedDisplayName(k->value(KIT_FINAL_NAME).toString());
    k->removeKey(KIT_TEMPORARY_NAME);
    k->removeKey(KIT_FINAL_NAME);

    foreach (const TemporaryInformationHandler &tih, m_temporaryHandlers) {
        const Core::Id fid = fullId(tih.id);
        const QVariantList temporaryValues = k->value(fid).toList();

        // Mark permanent in all other kits:
        foreach (Kit *ok, KitManager::kits()) {
            if (ok == k)
                continue;

            QVariantList otherTemporaryValues = ok->value(fid).toList();
            otherTemporaryValues = Utils::filtered(otherTemporaryValues, [&temporaryValues](const QVariant &v) {
                return temporaryValues.contains(v);
            });
            ok->setValueSilently(fid, otherTemporaryValues);
        }

        // persist:
        tih.persist(k, temporaryValues);
    }
}

void ProjectImporter::cleanupKit(Kit *k)
{
    foreach (const TemporaryInformationHandler &tih, m_temporaryHandlers) {
        const Core::Id fid = fullId(tih.id);
        QVariantList temporaryValues = k->value(fid).toList();
        temporaryValues = Utils::filtered(temporaryValues, [fid, k](const QVariant &v) {
           return !hasOtherUsers(fid, v, k);
        });
        tih.cleanup(k, temporaryValues);
    }
}

void ProjectImporter::addProject(Kit *k)
{
    if (!k->hasValue(KIT_IS_TEMPORARY))
        return;

    UpdateGuard guard(*this);
    QStringList projects = k->value(TEMPORARY_OF_PROJECTS, QStringList()).toStringList();
    projects.append(m_projectPath); // note: There can be more than one instance of the project added!
    k->setValueSilently(TEMPORARY_OF_PROJECTS, projects);
}

void ProjectImporter::removeProject(Kit *k)
{
    if (!k->hasValue(KIT_IS_TEMPORARY))
        return;

    UpdateGuard guard(*this);
    QStringList projects = k->value(TEMPORARY_OF_PROJECTS, QStringList()).toStringList();
    projects.removeOne(m_projectPath);

    if (projects.isEmpty())
        KitManager::deregisterKit(k);
    else
        k->setValueSilently(TEMPORARY_OF_PROJECTS, projects);
}

bool ProjectImporter::isTemporaryKit(Kit *k) const
{
    return k->hasValue(KIT_IS_TEMPORARY);
}

Kit *ProjectImporter::createTemporaryKit(const KitSetupFunction &setup) const
{
    Kit *k = new Kit;
    UpdateGuard guard(*this);
    {
        KitGuard kitGuard(k);
        k->setUnexpandedDisplayName(QCoreApplication::translate("ProjectExplorer::ProjectImporter", "Imported Kit"));;
        markKitAsTemporary(k);

        setup(k);

        // Set up values:
        foreach (KitInformation *ki, KitManager::kitInformation())
            ki->setup(k);
    } // ~KitGuard, sending kitUpdated
    KitManager::registerKit(k); // potentially adds kits to other targetsetuppages
    return k;
}

bool ProjectImporter::findTemporaryHandler(Core::Id id) const
{
    return Utils::contains(m_temporaryHandlers, [id](const TemporaryInformationHandler &ch) { return ch.id == id; });
}

void ProjectImporter::useTemporaryKitInformation(Core::Id id,
                                                 ProjectImporter::CleanupFunction cleanup,
                                                 ProjectImporter::PersistFunction persist)
{
    QTC_ASSERT(!findTemporaryHandler(id), return);
    m_temporaryHandlers.append({ id, cleanup, persist });
}

void ProjectImporter::addTemporaryData(Core::Id id, const QVariant &cleanupData, Kit *k) const
{
    QTC_ASSERT(findTemporaryHandler(id), return);
    const Core::Id fid = fullId(id);

    KitGuard guard(k);
    QVariantList tmp = k->value(fid).toList();
    QTC_ASSERT(!tmp.contains(cleanupData), return);
    tmp.append(cleanupData);
    k->setValue(fid, tmp);
}

bool ProjectImporter::hasKitWithTemporaryData(Core::Id id, const QVariant &data) const
{
    Core::Id fid = fullId(id);
    return Utils::contains(KitManager::kits(), [data, fid](Kit *k) {
        return k->value(fid).toList().contains(data);
    });
}

} // namespace ProjectExplorer
