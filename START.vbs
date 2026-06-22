' ============================================================================
' Tired Market - Silent Launcher
'
' Double-click this file to start Tired Market with NO console window.
'
' How this works:
'   VBScript runs via wscript.exe which has no console attached. We then
'   invoke START.bat with window style 0 (hidden), so START.bat handles the
'   python lookup but its console never becomes visible. Result: app starts
'   cleanly, no cmd flash, no leftover terminal.
'
' Portable: resolves its own folder via WScript.ScriptFullName, so it works
' from any drive / any folder name (desktop install, USB stick, renamed dir).
'
' If something is broken (Python missing, app file gone, etc), run START.bat
' directly instead - it's the visible launcher and will show the error.
' ============================================================================

Option Explicit

Dim WshShell, FSO, ScriptDir, BatPath

Set WshShell = CreateObject("WScript.Shell")
Set FSO = CreateObject("Scripting.FileSystemObject")

ScriptDir = FSO.GetParentFolderName(WScript.ScriptFullName)
BatPath = ScriptDir & "\START.bat"

If Not FSO.FileExists(BatPath) Then
    MsgBox "Tired Market launcher not found:" & vbCrLf & BatPath, _
           vbExclamation, "Tired Market"
    WScript.Quit 1
End If

WshShell.CurrentDirectory = ScriptDir
' Window style 0 = hidden (no console box). False = don't wait for exit.
WshShell.Run """" & BatPath & """", 0, False

Set WshShell = Nothing
Set FSO = Nothing
