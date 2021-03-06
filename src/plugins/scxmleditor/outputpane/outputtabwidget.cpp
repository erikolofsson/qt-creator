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

#include "outputtabwidget.h"
#include "outputpane.h"

#include <utils/qtcassert.h>

#include <QPainter>

using namespace ScxmlEditor::OutputPane;

PanePushButton::PanePushButton(OutputPane *pane, QWidget *parent)
    : QPushButton(parent)
{
    animator.setPropertyName("colorOpacity");
    animator.setTargetObject(this);

    setObjectName("PanePushButton");
    setCheckable(true);

    setText(pane->title());
    setIcon(pane->icon());

    connect(this, &PanePushButton::toggled, this, [this](bool toggled) {
        if (toggled)
            stopAlert();
    });

    connect(&animator, &QAbstractAnimation::finished, this, [this]() {
        m_animCounter++;
        if (m_animCounter < 8) {
            if (m_animCounter % 2 == 1)
                fadeOut();
            else
                fadeIn();
        }
    });

    connect(pane, &OutputPane::titleChanged, this, [=]() {
        setText(pane->title());
    });

    connect(pane, &OutputPane::iconChanged, this, [=]() {
        setIcon(pane->icon());
    });
}

void PanePushButton::startAlert(const QColor &color)
{
    m_color = color;
    m_animCounter = 0;
    fadeIn();
}

void PanePushButton::stopAlert()
{
    animator.stop();
}

void PanePushButton::fadeIn()
{
    animator.stop();
    animator.setDuration(300);
    animator.setStartValue(0);
    animator.setEndValue(80);
    animator.start();
}

void PanePushButton::fadeOut()
{
    animator.stop();
    animator.setDuration(300);
    animator.setStartValue(80);
    animator.setEndValue(0);
    animator.start();
}

void PanePushButton::setColorOpacity(int value)
{
    m_colorOpacity = value;
    update();
}

void PanePushButton::paintEvent(QPaintEvent *e)
{
    QPushButton::paintEvent(e);

    QPainter p(this);
    p.save();
    if (animator.state() != QAbstractAnimation::Stopped) {
        QRect r = rect();
        m_color.setAlpha(m_colorOpacity);
        p.setBrush(QBrush(m_color));
        p.setPen(Qt::NoPen);
        p.drawRect(r);
    }
    p.restore();
}

OutputTabWidget::OutputTabWidget(QWidget *parent)
    : QFrame(parent)
{
    m_ui.setupUi(this);
    close();
}

OutputTabWidget::~OutputTabWidget()
{
}

int OutputTabWidget::addPane(OutputPane *pane)
{
    if (pane) {
        auto button = new PanePushButton(pane, this);
        connect(button, &PanePushButton::clicked, this, &OutputTabWidget::buttonClicked);
        connect(pane, &OutputPane::dataChanged, this, &OutputTabWidget::showAlert);

        m_ui.m_buttonLayout->addWidget(button);
        m_ui.m_stackedWidget->addWidget(pane);

        m_buttons << button;
        m_pages << pane;

        return m_pages.count() - 1;
    }

    return -1;
}

void OutputTabWidget::showPane(OutputPane *pane)
{
    QTC_ASSERT(pane, return);

    m_ui.m_stackedWidget->setCurrentWidget(pane);
    m_buttons[m_pages.indexOf(pane)]->setChecked(true);
    pane->setPaneFocus();
    if (!m_ui.m_stackedWidget->isVisible()) {
        m_ui.m_stackedWidget->setVisible(true);
        emit visibilityChanged(true);
    }
}

void OutputTabWidget::showPane(int index)
{
    showPane(static_cast<OutputPane*>(m_ui.m_stackedWidget->widget(index)));
}

void OutputTabWidget::close()
{
    m_ui.m_stackedWidget->setVisible(false);
    emit visibilityChanged(false);
}

void OutputTabWidget::showAlert()
{
    int index = m_pages.indexOf(qobject_cast<OutputPane*>(sender()));
    if (index >= 0 && !m_buttons[index]->isChecked())
        m_buttons[index]->startAlert(m_pages[index]->alertColor());
}

void OutputTabWidget::buttonClicked(bool para)
{
    int index = m_buttons.indexOf(qobject_cast<PanePushButton*>(sender()));
    if (index >= 0) {
        if (para) {
            for (int i = 0; i < m_buttons.count(); ++i) {
                if (i != index)
                    m_buttons[i]->setChecked(false);
            }
            showPane(index);
        } else
            close();
    }
}
