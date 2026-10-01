Set ws = CreateObject("WScript.Shell")
startup = ws.SpecialFolders("Startup")
Set lnk = ws.CreateShortcut(startup & "\school-radar.lnk")
lnk.TargetPath = "C:\Users\YOURNAME\deploy-work\school-radar-pkg\school-radar\school-radar.exe"
lnk.WorkingDirectory = "C:\Users\YOURNAME\deploy-work\school-radar-pkg\school-radar"
lnk.Description = "school-radar 校园通知抓取"
lnk.Save
WScript.Echo "created: " & startup & "\school-radar.lnk"
