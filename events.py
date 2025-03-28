"""Events used througout the plugin."""

from wx.lib.newevent import NewEvent  # pylint: disable=import-error

DownloadStartedEvent, EVT_DOWNLOAD_STARTED_EVENT = NewEvent()
DownloadProgressEvent, EVT_DOWNLOAD_PROGRESS_EVENT = NewEvent()
DownloadCompletedEvent, EVT_DOWNLOAD_COMPLETED_EVENT = NewEvent()

UnzipCombiningStartedEvent, EVT_UNZIP_COMBINING_STARTED_EVENT = NewEvent()
UnzipCombiningProgressEvent, EVT_UNZIP_COMBINING_PROGRESS_EVENT = NewEvent()
UnzipExtractingStartedEvent, EVT_UNZIP_EXTRACTING_STARTED_EVENT = NewEvent()
UnzipExtractingProgressEvent, EVT_UNZIP_EXTRACTING_PROGRESS_EVENT = NewEvent()
UnzipExtractingCompletedEvent, EVT_UNZIP_EXTRACTING_COMPLETED_EVENT = NewEvent()

MessageEvent, EVT_MESSAGE_EVENT = NewEvent()
AssignPartsEvent, EVT_ASSIGN_PARTS_EVENT = NewEvent()
PopulateFootprintListEvent, EVT_POPULATE_FOOTPRINT_LIST_EVENT = NewEvent()
UpdateSetting, EVT_UPDATE_SETTING = NewEvent()
LogboxAppendEvent, EVT_LOGBOX_APPEND_EVENT = NewEvent()
