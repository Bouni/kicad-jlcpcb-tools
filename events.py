"""Events used througout the plugin."""

# pyright: reportMissingImports=false, reportMissingModuleSource=false

try:
    from wx.lib.newevent import NewEvent  # pylint: disable=import-error
except ImportError:  # pragma: no cover - test environments may not have wx
    def NewEvent():
        """Fallback event factory for non-wx test environments."""

        class _DummyEvent:
            def __init__(self, **kwargs):
                for key, value in kwargs.items():
                    setattr(self, key, value)

        return _DummyEvent, object()

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
AssemblyEnrichmentProgressEvent, EVT_ASSEMBLY_ENRICHMENT_PROGRESS_EVENT = NewEvent()
AssemblyEnrichmentCompletedEvent, EVT_ASSEMBLY_ENRICHMENT_COMPLETED_EVENT = NewEvent()
BomDataChangedEvent, EVT_BOM_DATA_CHANGED_EVENT = NewEvent()
