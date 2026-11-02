from __future__ import annotations

from typing import TYPE_CHECKING

from PySide6.QtCore import QFileInfo, QObject, QRectF, QSize, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QFont, QFontMetrics, QPainter, QPixmap
from PySide6.QtWidgets import (
    QApplication, QDialog, QFileIconProvider, QHBoxLayout, QSizePolicy, QVBoxLayout, QWidget,
)
from qframelesswindow import FramelessDialog
from qfluentwidgets import (
    CaptionLabel, FluentIcon, FluentStyleSheet, FluentTitleBar, ImageLabel, ProgressBar,
    SimpleCardWidget, SubtitleLabel, isDarkTheme, setFont, themeColor,
)

from app.config.cfg import cfg
from app.format import toReadableSize, toReadableTime
from app.platform.desktop import raiseWindow

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
NAME_LINES = 2
TEXT_WIDTH = WINDOW_WIDTH - 2 * CONTENT_MARGIN - 2 * CARD_PADDING - ICON_SIZE - ICON_SPACING
# 提前留出余量，避免 QLabel 对已经排好的行再折一次
NAME_SAFE_MARGIN = 8
BAR_HEIGHT = 10
PERCENT_WIDTH = 86
REFRESH_INTERVAL = 500


class SegmentedProgressBar(QWidget):
    """按分片区间绘制的进度条，用于支持多分片下载的 HTTP 任务。"""

    def __init__(self, task: Task, parent=None):
        super().__init__(parent)
        self._task = task
        self._spans: list[tuple[float, float]] = []
        self.setFixedHeight(BAR_HEIGHT)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)

    @staticmethod
    def hasSubworkers(task: Task) -> bool:
        return any(getattr(step, "subworkers", None) is not None for step in task.steps)

    @classmethod
    def canRenderRanges(cls, task: Task) -> bool:
        if task.fileSize <= 0:
            return False
        steps = [step for step in task.steps if getattr(step, "subworkers", None) is not None]
        if not steps:
            return False
        for step in steps:
            if step.fileSize <= 0 or not step.subworkers:
                return False
            if not any(subworker.end >= subworker.start for subworker in step.subworkers):
                return False
        return True

    def refresh(self) -> None:
        spans = self._toSpans()
        if spans != self._spans:
            self._spans = spans
            self.update()

    def _toSpans(self) -> list[tuple[float, float]]:
        steps = [
            step for step in self._task.steps
            if getattr(step, "subworkers", None) is not None and step.fileSize > 0
        ]
        total = sum(step.fileSize for step in steps)
        if total <= 0:
            return []

        spans: list[tuple[float, float]] = []
        offset = 0
        for step in steps:
            size = step.fileSize
            for subworker in step.subworkers:
                start = min(max(subworker.start, 0), size)
                end = min(subworker.end + 1, subworker.position, size)
                if end > start:
                    spans.append(((offset + start) / total, (offset + end) / total))
            offset += size

        spans.sort()
        merged: list[tuple[float, float]] = []
        for start, end in spans:
            if merged and start <= merged[-1][1]:
                merged[-1] = (merged[-1][0], max(merged[-1][1], end))
            else:
                merged.append((start, end))
        return merged

    def paintEvent(self, event) -> None:
        painter = QPainter(self)
        painter.setRenderHints(QPainter.RenderHint.Antialiasing)
        painter.setPen(Qt.PenStyle.NoPen)

        width = self.width()
        height = self.height()
        radius = height / 2
        painter.setBrush(QColor(255, 255, 255, 30) if isDarkTheme() else QColor(0, 0, 0, 26))
        painter.drawRoundedRect(QRectF(0, 0, width, height), radius, radius)

        painter.setBrush(themeColor())
        for start, end in self._spans:
            x = start * width
            spanWidth = max((end - start) * width, 1.0)
            spanRadius = min(radius, spanWidth / 2)
            painter.drawRoundedRect(QRectF(x, 0, spanWidth, height), spanRadius, spanRadius)


class TrackProgressBar(ProgressBar):
    """给 qfluentwidgets 进度条补一条与分片进度条一致的圆角底槽。"""

    def paintEvent(self, event) -> None:
        painter = QPainter(self)
        painter.setRenderHints(QPainter.RenderHint.Antialiasing)
        painter.setPen(Qt.PenStyle.NoPen)

        width = self.width()
        height = self.height()
        radius = height / 2
        painter.setBrush(QColor(255, 255, 255, 30) if isDarkTheme() else QColor(0, 0, 0, 26))
        painter.drawRoundedRect(QRectF(0, 0, width, height), radius, radius)

        if self.minimum() >= self.maximum():
            return
        ratio = min(max(self.val / (self.maximum() - self.minimum()), 0.0), 1.0)
        if ratio <= 0:
            return
        painter.setBrush(self.barColor())
        painter.drawRoundedRect(QRectF(0, 0, width * ratio, height), radius, radius)


class TaskProgressDialog(FramelessDialog):
    dismissedByUser = Signal()

    def __init__(self, task: Task):
        super().__init__()
        self._task = task
        self._isClosingProgrammatically = False
        self._hasEmittedDismissal = False
        self._isSegmented: bool | None = None
        self._hasSubworkers = SegmentedProgressBar.hasSubworkers(task)

        self.card = SimpleCardWidget(self)
        self.iconLabel = ImageLabel(self.card)
        self.nameLabel = SubtitleLabel(self.card)
        self.pathLabel = CaptionLabel(self.card)
        self.progressBar = TrackProgressBar(self)
        self.segmentedBar = SegmentedProgressBar(task, self)
        self.percentLabel = SubtitleLabel(self)
        self.statusLabel = CaptionLabel(self)
        self.fragmentLabel = CaptionLabel(self)
        self.refreshTimer = QTimer(self)
        self.contentLayout = QVBoxLayout(self)
        self.bodyLayout = QVBoxLayout()
        self.progressLayout = QVBoxLayout()
        self.barLayout = QHBoxLayout()
        self.statusLayout = QHBoxLayout()

        self._initWidget()
        self._initLayout()
        self._bind()
        self.refresh()
        self.setFixedSize(WINDOW_WIDTH, self.sizeHint().height())

    @property
    def task(self) -> Task:
        return self._task

    def _initWidget(self) -> None:
        self.setResizeEnabled(False)
        titleBar = FluentTitleBar(self)
        self.setTitleBar(titleBar)
        self.titleBar.maxBtn.hide()
        self.titleBar.iconLabel.hide()
        self.titleBar.setDoubleClickEnabled(False)
        self.titleBar.setFixedHeight(TITLE_BAR_HEIGHT)
        self.setWindowTitle(self.tr("正在下载"))
        FluentStyleSheet.DIALOG.apply(self)

        setFont(self.nameLabel, 18, QFont.Weight.DemiBold)
        self.nameLabel.setWordWrap(True)
        self.nameLabel.setFixedWidth(TEXT_WIDTH)
        self.nameLabel.setToolTip(self._task.name)

        for label in (self.pathLabel, self.statusLabel, self.fragmentLabel):
            label.setTextColor(QColor(96, 96, 96), QColor(206, 206, 206))

        self.iconLabel.setPixmap(self._toIconPixmap())
        self.iconLabel.setFixedSize(ICON_SIZE, ICON_SIZE)

        self.nameLabel.setText(self._toNameText())
        self.pathLabel.setText(self._toPathText())
        self.pathLabel.setToolTip(self._task.outputPath)

        setFont(self.percentLabel, 20, QFont.Weight.DemiBold)
        self.percentLabel.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        self.percentLabel.setFixedWidth(PERCENT_WIDTH)
        self.percentLabel.setTextColor(themeColor())

        self.progressBar.setFixedHeight(BAR_HEIGHT)
        self.progressBar.setUseAni(False)
        self.progressBar.hide()
        self.segmentedBar.hide()

        self.fragmentLabel.setVisible(self._hasSubworkers)

    def _initLayout(self) -> None:
        textLayout = QVBoxLayout()
        textLayout.setContentsMargins(0, 0, 0, 0)
        textLayout.setSpacing(4)
        textLayout.addWidget(self.nameLabel)
        textLayout.addWidget(self.pathLabel)

        cardLayout = QHBoxLayout(self.card)
        cardLayout.setContentsMargins(CARD_PADDING, CARD_PADDING, CARD_PADDING, CARD_PADDING)
        cardLayout.setSpacing(ICON_SPACING)
        cardLayout.addWidget(self.iconLabel, 0, Qt.AlignmentFlag.AlignTop)
        cardLayout.addLayout(textLayout, 1)

        self.barLayout.setContentsMargins(0, 0, 0, 0)
        self.barLayout.setSpacing(16)
        self.barLayout.addWidget(self.progressBar, 1, Qt.AlignmentFlag.AlignVCenter)
        self.barLayout.addWidget(self.segmentedBar, 1, Qt.AlignmentFlag.AlignVCenter)
        self.barLayout.addWidget(self.percentLabel, 0)

        self.statusLayout.setContentsMargins(0, 0, 0, 0)
        self.statusLayout.setSpacing(12)
        self.statusLayout.addWidget(self.statusLabel, 1)
        self.statusLayout.addWidget(self.fragmentLabel, 0, Qt.AlignmentFlag.AlignRight)

        self.progressLayout.setContentsMargins(0, 0, 0, 0)
        self.progressLayout.setSpacing(10)
        self.progressLayout.addLayout(self.barLayout)
        self.progressLayout.addLayout(self.statusLayout)

        self.bodyLayout.setContentsMargins(
            CONTENT_MARGIN, CONTENT_VERTICAL_MARGIN, CONTENT_MARGIN, CONTENT_VERTICAL_MARGIN)
        self.bodyLayout.setSpacing(16)
        self.bodyLayout.addWidget(self.card)
        self.bodyLayout.addLayout(self.progressLayout)

        self.contentLayout.setContentsMargins(0, TITLE_BAR_HEIGHT, 0, 0)
        self.contentLayout.setSpacing(0)
        self.contentLayout.addLayout(self.bodyLayout)

    def _bind(self) -> None:
        self.refreshTimer.setInterval(REFRESH_INTERVAL)
        self.refreshTimer.timeout.connect(self.refresh)
        self.refreshTimer.start()
        self.finished.connect(self._onFinished)

    def refresh(self) -> None:
        progress, speed, receivedBytes = self._task.currentSnapshot()
        progress = min(max(progress, 0.0), 100.0)
        self.percentLabel.setText(f"{progress:.1f}%")
        self._refreshBar(progress)
        self.statusLabel.setText(self._toStatusText(speed, receivedBytes))
        if self._hasSubworkers:
            count = sum(len(getattr(step, "subworkers", ())) for step in self._task.steps)
            self.fragmentLabel.setText(self.tr("分片：{0}").format(count))
            self.fragmentLabel.setToolTip(self.tr("当前活动分片：{0}").format(count))

    def _refreshBar(self, progress: float) -> None:
        isSegmented = SegmentedProgressBar.canRenderRanges(self._task)
        if isSegmented != self._isSegmented:
            self._isSegmented = isSegmented
            self.segmentedBar.setVisible(isSegmented)
            self.progressBar.setVisible(not isSegmented)
        if isSegmented:
            self.segmentedBar.refresh()
        else:
            self.progressBar.setValue(round(progress))

    def _toStatusText(self, speed: int, receivedBytes: int) -> str:
        parts = [f"{toReadableSize(speed)}/s"]
        fileSize = self._task.fileSize
        if fileSize > 0:
            parts.append(f"{toReadableSize(receivedBytes)} / {toReadableSize(fileSize)}")
            if speed > 0:
                remaining = max(0, int((fileSize - receivedBytes) / speed))
                parts.append(self.tr("剩余 {0}").format(toReadableTime(remaining)))
        return " · ".join(parts)

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
        if metrics.horizontalAdvance(rest) <= limit:
            return rest

        line = self._toLine(metrics, rest, limit)
        tail = rest[len(line):].lstrip()
        # 只比一行多一点时均分成两行，避免第二行只剩扩展名
        if tail and metrics.horizontalAdvance(tail) <= limit * 0.4:
            return "\n".join(self._toBalancedLines(metrics, rest))

        lines: list[str] = [line]
        rest = tail
        while rest and len(lines) < NAME_LINES:
            if len(lines) == NAME_LINES - 1:
                lines.append(metrics.elidedText(rest, Qt.TextElideMode.ElideRight, limit))
                break
            line = self._toLine(metrics, rest, limit)
            lines.append(line)
            rest = rest[len(line):].lstrip()
        return "\n".join(lines)

    def _toBalancedLines(self, metrics: QFontMetrics, text: str) -> list[str]:
        limit = TEXT_WIDTH - NAME_SAFE_MARGIN
        target = metrics.horizontalAdvance(text) / 2
        cut = 1
        while cut < len(text) - 1 and metrics.horizontalAdvance(text[:cut + 1]) <= target:
            cut += 1

        # 优先在切点附近的分隔符之后断行，避免把单词或扩展名拆开
        for separator in (" ", ".", "_", "-"):
            position = text.find(separator, cut)
            if position != -1 and position <= cut + 4 and metrics.horizontalAdvance(text[:position + 1]) <= limit:
                cut = position + 1
                break

        cut = min(max(cut, 1), len(text) - 1)
        first = text[:cut].rstrip() or text[:1]
        second = text[cut:].lstrip()
        return [first, second] if second else [first]

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

    def closeProgrammatically(self) -> None:
        self._isClosingProgrammatically = True
        self.refreshTimer.stop()
        self.done(QDialog.DialogCode.Rejected)

    def reject(self) -> None:
        self._emitDismissed()
        super().reject()

    def closeEvent(self, event) -> None:
        self._emitDismissed()
        super().closeEvent(event)

    def _emitDismissed(self) -> None:
        if self._isClosingProgrammatically or self._hasEmittedDismissal:
            return
        self._hasEmittedDismissal = True
        self.dismissedByUser.emit()

    def _onFinished(self, *_) -> None:
        self.refreshTimer.stop()
        self.deleteLater()


class TaskProgressPrompt(QObject):

    def __init__(self, taskService: TaskService):
        super().__init__()
        self._queue: list[Task] = []
        self._dialog: TaskProgressDialog | None = None
        taskService.taskStarted.connect(self._onTaskStarted)
        taskService.taskPaused.connect(self._onTaskStopped)
        taskService.taskCompleted.connect(self._onTaskStopped)
        taskService.taskFailed.connect(self._onTaskStopped)
        taskService.taskRemoved.connect(self._onTaskRemoved)
        cfg.shouldShowProgressDialog.valueChanged.connect(self._onShouldShowChanged)

    def _onTaskStarted(self, task: Task) -> None:
        if not cfg.shouldShowProgressDialog.value:
            return
        if self._dialog is not None and self._dialog.task.taskId == task.taskId:
            return
        if any(queued.taskId == task.taskId for queued in self._queue):
            return
        self._queue.append(task)
        self._showNext()

    def _onTaskStopped(self, task: Task) -> None:
        self._queue = [queued for queued in self._queue if queued.taskId != task.taskId]
        if self._dialog is not None and self._dialog.task.taskId == task.taskId:
            self._closeDialog()

    def _onTaskRemoved(self, taskId: str) -> None:
        self._queue = [queued for queued in self._queue if queued.taskId != taskId]
        if self._dialog is not None and self._dialog.task.taskId == taskId:
            self._closeDialog()

    def _onDismissedByUser(self) -> None:
        if self._dialog is not None:
            taskId = self._dialog.task.taskId
            self._queue = [queued for queued in self._queue if queued.taskId != taskId]

    def _onShouldShowChanged(self, isEnabled: bool) -> None:
        if isEnabled:
            return
        self._queue.clear()
        if self._dialog is not None:
            self._closeDialog()

    def _showNext(self) -> None:
        if self._dialog is not None or not self._queue:
            return
        if not cfg.shouldShowProgressDialog.value:
            self._queue.clear()
            return
        dialog = TaskProgressDialog(self._queue.pop(0))
        self._dialog = dialog
        dialog.dismissedByUser.connect(self._onDismissedByUser)
        dialog.destroyed.connect(self._onDialogDestroyed)
        dialog.show()
        raiseWindow(dialog)

    def _closeDialog(self) -> None:
        dialog = self._dialog
        if dialog is None:
            return
        try:
            dialog.closeProgrammatically()
        except RuntimeError:
            self._dialog = None
            self._showNext()

    def _onDialogDestroyed(self, *_) -> None:
        self._dialog = None
        self._showNext()
