import wx

from .mainwindow import JLCPCBTools, BoardProvider


# stub out just enough for the GUI to come up. add more as needed
class BoardStub:
    def GetFileName(self):
        return "fake_test_board.kicad_pcb"

    def GetFootprints(self):
        return []


# a board provider that returns a stubbed out version of Kicad's 'Board' class.
# don't expect this to work well. for dev/testing only
class StandaloneBoardProvider(BoardProvider):
    def get_board(self):
        return BoardStub()


if __name__ == '__main__':
    print("starting jlcpcbtools standalone mode...")

    # this is a debug-only way to allow the UI to come up without starting Kicad directly
    # use at your own risk. useful for opening project in debugger (like PyCharm/Jetbrains/vscode/etc)
    # launch this as a python module like this:
    # 1) command line: "C:\Program Files\KiCad\6.0\bin\python.exe -m kicad-jlcpcb-tools"
    #    note: the text after the -m is the directory name. change to match yours.
    #    in live Kicad editing, mine looked liked "com_github_bouni_kicad-jlcpcb-tools".
    #    use the python interpreter for kicad.
    # 2) set the working directory to the kicad root plugin directory
    #    i.e. "C:\{your_home_dir}\Documents\KiCad\6.0\3rdparty\plugins\"
    # 3) set environment var WXSUPPRESS_SIZER_FLAGS_CHECK=1
    # 4) WxWidgets will probably complain that Kicad isn't actually started here,
    #    hit "No" when it asks if you want to stop

    # if using PyCharm or Jetbrains IDEs, set the interpreter to Kicad6's python.exe, and
    # under run configuration, select Python.  click on "script path" and change instead to "module name",
    # type in what you would have typed under the "-m" flag above.

    app = wx.App(None)

    dialog = JLCPCBTools(None, board_provider=StandaloneBoardProvider())
    dialog.Center()
    dialog.Show()

    app.MainLoop()
