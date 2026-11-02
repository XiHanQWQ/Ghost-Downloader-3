from __future__ import annotations

from typing import TYPE_CHECKING

from PySide6.QtCore import QFileInfo, QObject, QSize, Qt
from PySide6.QtGui import QColor, QFont, QFontMetrics, QPixmap
from PySide6.QtWidgets import QApplication, QFileIconProvider, QHBoxLayout, QVBoxLayout
from qframelesswindow import FramelessDialog
from qfluentwidgets import (
    CaptionLabel, FluentIcon, FluentStyleSheet, FluentTitleBar, HorizontalSeparator, ImageLabel,
    PrimaryPushButton, PushButton, SimpleCardWidget, SubtitleLabel, setFont,
)

from app.config.cfg import cfg
from app.format import toReadableSize
from app.models.task import TaskStatus
from app.platform.desktop import openFile, raiseWindow, revealInFolder
from app.platform.filesystem import isExisting

if TYPE_CHECKING:
    from app.models.task import Task
    from app.services.task_service import TaskService


WINDOW_WIDTH = 520
TITLE_BAR_HEIGHT = 30
CONTENT_MARGIN = 24
CONTENT_VERTICAL_MARGIN = 20
CARD_PADDING = 16
ICON_SIZE = 48
ICON_SPACING = 16
BUTTON_HEIGHT = 36
CONTENT_SPACING = 16
NAME_LINES = 2
TEXT_WIDTH = WINDOW_WIDTH - 2 * CONTENT_MARGIN - 2 * CARD_PADDING - ICON_SIZE - ICON_SPACING
# 提前留出余量，避免 QLabel 对已经排好的行再折一次
NAME_SAFE_MARGIN = 8


class TaskCompletedDialog(FramelessDialog):

    def __init__(self, task: Task):
        super().__init__()
        self._task = task

        self.card = SimpleCardWidget(self)
        self.iconLabel = ImageLabel(self.card)
        self.nameLabel = SubtitleLabel(self.card)
        self.sizeLabel = CaptionLabel(self.card)
        self.pathLabel = CaptionLabel(self.card)
        self.openButton = PrimaryPushButton(FluentIcon.LINK, self.tr("打开文件"), self)
        self.folderButton = PushButton(FluentIcon.FOLDER, self.tr("打开文件夹"), self)
        self.separator = HorizontalSeparator(self)
        self.contentLayout = QVBoxLayout(self)
        self.bodyLayout = QVBoxLayout()
        self.cardLayout = QHBoxLayout(self.card)
        self.textLayout = QVBoxLayout()
        self.buttonLayout = QHBoxLayout()

        self._initWidget()
        self._initLayout()
        self._bind()

    def _initWidget(self) -> None:
        self.setResizeEnabled(False)
        titleBar = FluentTitleBar(self)
        self.setTitleBar(titleBar)
        self.titleBar.maxBtn.hide()
        self.titleBar.iconLabel.hide()
        self.titleBar.setDoubleClickEnabled(False)
        self.titleBar.setFixedHeight(TITLE_BAR_HEIGHT)
        self.setWindowTitle(self.tr("下载完成"))
        FluentStyleSheet.DIALOG.apply(self)

        setFont(self.nameLabel, 18, QFont.Weight.DemiBold)
        self.nameLabel.setWordWrap(True)
        self.nameLabel.setFixedWidth(TEXT_WIDTH)
        self.nameLabel.setToolTip(self._task.name)
        for label in (self.sizeLabel, self.pathLabel):
            label.setTextColor(QColor(96, 96, 96), QColor(206, 206, 206))

        self.iconLabel.setPixmap(self._toIconPixmap())
        self.iconLabel.setFixedSize(ICON_SIZE, ICON_SIZE)

        self.nameLabel.setText(self._toNameText())
        self.sizeLabel.setText(self.tr("大小：{0}").format(
            toReadableSize(self._task.fileSize) if self._task.fileSize > 0 else self.tr("未知大小")))
        self.pathLabel.setText(self._toPathText())
        self.pathLabel.setToolTip(self._task.outputPath)

        for button in (self.openButton, self.folderButton):
            button.setFixedHeight(BUTTON_HEIGHT)
            button.setAutoDefault(True)
        self.openButton.setDefault(True)
        self.openButton.setFocus()

    def _initLayout(self) -> None:
        self.textLayout.setContentsMargins(0, 0, 0, 0)
        self.textLayout.setSpacing(4)
        self.textLayout.addWidget(self.nameLabel)
        self.textLayout.addWidget(self.sizeLabel)
        self.textLayout.addWidget(self.pathLabel)

        self.cardLayout.setContentsMargins(CARD_PADDING, CARD_PADDING, CARD_PADDING, CARD_PADDING)
        self.cardLayout.setSpacing(ICON_SPACING)
        self.cardLayout.addWidget(self.iconLabel, 0, Qt.AlignmentFlag.AlignTop)
        self.cardLayout.addLayout(self.textLayout, 1)

        self.buttonLayout.setContentsMargins(
            CONTENT_MARGIN, CONTENT_SPACING, CONTENT_MARGIN, CONTENT_VERTICAL_MARGIN)
        self.buttonLayout.setSpacing(12)
        self.buttonLayout.addWidget(self.openButton, 1)
        self.buttonLayout.addWidget(self.folderButton, 1)

        self.bodyLayout.setContentsMargins(
            CONTENT_MARGIN, CONTENT_VERTICAL_MARGIN, CONTENT_MARGIN, CONTENT_SPACING)
        self.bodyLayout.addWidget(self.card)

        self.contentLayout.setContentsMargins(0, TITLE_BAR_HEIGHT, 0, 0)
        self.contentLayout.setSpacing(0)
        self.contentLayout.addLayout(self.bodyLayout)
        self.contentLayout.addWidget(self.separator)
        self.contentLayout.addLayout(self.buttonLayout)

        self.setFixedSize(WINDOW_WIDTH, self.sizeHint().height())

    def _bind(self) -> None:
        self.openButton.clicked.connect(self._onOpenFile)
        self.folderButton.clicked.connect(self._onOpenFolder)
        self.finished.connect(self.deleteLater)

    def _toIconPixmap(self) -> QPixmap:
        provider = QFileIconProvider()
        icon = (
            provider.icon(QFileIconProvider.IconType.Folder) if self._task.isOutputFolder
            else provider.icon(QFileInfo(self._task.outputPath))
        )
        screen = self.screen() or QApplication.primaryScreen()
        dpr = screen.devicePixelRatio() if screen is not None else 1.0
        size = QSize(round(ICON_SIZE * dpr), round(ICON_SIZE * dpr))
        pixmap = icon.pixmap(size) if not icon.isNull() else QPixmap()
        return pixmap if not pixmap.isNull() else FluentIcon.DOCUMENT.icon().pixmap(size)

    def _toNameText(self) -> str:
        metrics = QFontMetrics(self.nameLabel.font())
        limit = TEXT_WIDTH - NAME_SAFE_MARGIN
        rest = self._task.name.strip() or self.tr("未命名")
        lines: list[str] = []
        while rest and len(lines) < NAME_LINES:
            if len(lines) == NAME_LINES - 1:
                lines.append(metrics.elidedText(rest, Qt.TextElideMode.ElideRight, limit))
                break
            line = self._toLine(metrics, rest, limit)
            lines.append(line)
            rest = rest[len(line):].lstrip()
        return "\n".join(lines)

    def _toLine(self, metrics: QFontMetrics, text: str, limit: int) -> str:
        cut = len(text)
        while cut > 0 and metrics.horizontalAdvance(text[:cut]) > limit:
            cut -= 1

        if cut >= len(text):
            return text

        # 优先在常见的文件名分隔符处断行，但不让首行过短
        for separator in (" ", ".", "_", "-", "、", "，", "。"):
            position = text.rfind(separator, 0, cut + 1)
            if position >= cut * 2 // 3:
                cut = position + 1
                break

        return text[:cut] or text[:1]

    def _toPathText(self) -> str:
        prefix = self.tr("保存位置：")
        metrics = QFontMetrics(self.pathLabel.font())
        path = metrics.elidedText(
            self._task.outputPath, Qt.TextElideMode.ElideMiddle,
            TEXT_WIDTH - metrics.horizontalAdvance(prefix),
        )
        return prefix + path

    def _onOpenFile(self) -> None:
        openFile(self._task.outputPath)
        self.close()

    def _onOpenFolder(self) -> None:
        revealInFolder(self._task.outputPath)
        self.close()


class TaskCompletedPrompt(QObject):

    def __init__(self, taskService: TaskService):
        super().__init__()
        self._queue: list[Task] = []
        self._dialog: TaskCompletedDialog | None = None
        taskService.taskCompleted.connect(self._onTaskCompleted)

    def _onTaskCompleted(self, task: Task) -> None:
        if not cfg.shouldShowCompletionDialog.value:
            return
        if task.status != TaskStatus.COMPLETED or not task.hasOutputFile:
            return
        if not isExisting(task.outputPath):
            return
        self._queue.append(task)
        self._showNext()

    def _showNext(self) -> None:
        if self._dialog is not None or not self._queue:
            return
        dialog = TaskCompletedDialog(self._queue.pop(0))
        self._dialog = dialog
        dialog.destroyed.connect(self._onDialogDestroyed)
        dialog.show()
        raiseWindow(dialog)

    def _onDialogDestroyed(self, *_) -> None:
        self._dialog = None
        self._showNext()
