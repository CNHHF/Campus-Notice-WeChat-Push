' Create a desktop shortcut that starts the push pipeline (start-all.bat).
' Put start-all.bat in %USERPROFILE%\deploy-work\ first (or edit the paths
' below), then double-click this script. Rename the shortcut if you like.
Set ws = CreateObject("WScript.Shell")
desktop = ws.SpecialFolders("Desktop")
Set lnk = ws.CreateShortcut(desktop & "\Campus-Notice-Push.lnk")
lnk.TargetPath = ws.ExpandEnvironmentStrings("%USERPROFILE%") & "\deploy-work\start-all.bat"
lnk.WorkingDirectory = ws.ExpandEnvironmentStrings("%USERPROFILE%") & "\deploy-work"
lnk.Description = "start campus notice push pipeline"
lnk.Save
WScript.Echo "created: " & desktop & "\Campus-Notice-Push.lnk"
