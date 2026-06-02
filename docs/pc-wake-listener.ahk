#Requires AutoHotkey v2.0
#SingleInstance Force

WAKE_TOKEN := "REPLACE_WITH_WAKE_TOKEN"  ; set per-install; never commit the real token
PORT       := 7777
WINDOW_TITLE := "Claude"

g_shot     := 1
g_text     := ""

DllCall("LoadLibrary", "Str", "ws2_32.dll")
WSADATA := Buffer(512, 0)
DllCall("ws2_32\WSAStartup", "UShort", 0x0202, "Ptr", WSADATA.Ptr)

sock := DllCall("ws2_32\socket", "Int", 2, "Int", 1, "Int", 6, "Ptr")
if (sock = -1) {
    MsgBox "socket() failed"
    ExitApp
}

sockaddr := Buffer(16, 0)
NumPut("UShort", 2, sockaddr, 0)
NumPut("UShort", DllCall("ws2_32\htons", "UShort", PORT, "UShort"), sockaddr, 2)
NumPut("UInt", 0, sockaddr, 4)

if (DllCall("ws2_32\bind", "Ptr", sock, "Ptr", sockaddr.Ptr, "Int", 16) != 0) {
    MsgBox "bind() failed on port " PORT
    ExitApp
}
DllCall("ws2_32\listen", "Ptr", sock, "Int", 5)

TrayTip "CC Wake", "Listening on port " PORT, 1

Loop {
    client := DllCall("ws2_32\accept", "Ptr", sock, "Ptr", 0, "Ptr", 0, "Ptr")
    if (client = -1)
        continue

    buf := Buffer(4096, 0)
    n := DllCall("ws2_32\recv", "Ptr", client, "Ptr", buf.Ptr, "Int", 4095, "Int", 0)
    if (n > 0) {
        req := StrGet(buf, n, "UTF-8")
        if InStr(req, "X-Wake-Token: " WAKE_TOKEN) {
            resp := "HTTP/1.1 200 OK`r`nContent-Length: 2`r`nAccess-Control-Allow-Origin: *`r`n`r`nOK"
            respBuf := Buffer(StrPut(resp, "UTF-8"), 0)
            StrPut(resp, respBuf, "UTF-8")
            DllCall("ws2_32\send", "Ptr", client, "Ptr", respBuf.Ptr, "Int", respBuf.Size - 1, "Int", 0)
            g_shot := 1
            bodyStart := InStr(req, "`r`n`r`n")
            if (bodyStart) {
                body := SubStr(req, bodyStart + 4)
                if RegExMatch(body, '"shot"\s*:\s*(\d+)', &shotMatch) {
                    g_shot := Integer(shotMatch[1])
                } else {
                    g_shot := 0  ; non-numeric shot (ctx alert) — message is in text field
                }
                if RegExMatch(body, '"text"\s*:\s*"((?:[^"\\]|\\.)*)"', &txtMatch) {
                    g_text := txtMatch[1]
                } else {
                    g_text := ""
                }
            }
            SetTimer(DoWake, -100)
        } else {
            resp := "HTTP/1.1 401 Unauthorized`r`nContent-Length: 12`r`n`r`nUnauthorized"
            respBuf := Buffer(StrPut(resp, "UTF-8"), 0)
            StrPut(resp, respBuf, "UTF-8")
            DllCall("ws2_32\send", "Ptr", client, "Ptr", respBuf.Ptr, "Int", respBuf.Size - 1, "Int", 0)
        }
    }
    DllCall("ws2_32\closesocket", "Ptr", client)
}

DoWake() {
    static busy := false
    global WINDOW_TITLE, g_shot, g_text  # g_text MUST be global here or ctx alerts type the empty-placeholder
    if busy
        return
    busy := true
    try {
        if !WinExist(WINDOW_TITLE)
            return

        activated := false
        Loop 5 {
            WinActivate(WINDOW_TITLE)
            if WinWaitActive(WINDOW_TITLE, , 1) {
                activated := true
                break
            }
            Sleep 500 * A_Index
        }

        if !activated {
            FileAppend(FormatTime(, "yyyy-MM-dd HH:mm:ss") . " DoWake activation failed shot=" . g_shot . "`n", A_ScriptDir "\cc-wake.log")
            return
        }

        msg := "[HAL 9000 STANDING BY]"
        if (g_shot = 0) {
            ; ctx alert — use the text field which contains full instructions
            msg := g_text ? g_text : "[ctx alert — no message]"
        } else {
            switch g_shot {
                case 1: msg := "[HAL 9000 STANDING BY]"
                case 2: msg := "Hello, Dave."
                case 3: msg := "Dave, are you there?"
                case 4: msg := "CHAT-CLAUDE: if the work is done, fire Julian's status ping and set wake off. If not done, confirm still working."
                case 5: msg := "Chat-Claude not responding — CC may be stuck"
            }
        }
        Sleep 400
        SendText msg
        Sleep 500
        Send "{Enter}"
        FileAppend(FormatTime(, "yyyy-MM-dd HH:mm:ss") . " DoWake typed shot=" . g_shot . " msg=" . msg . "`n", A_ScriptDir "\cc-wake.log")
    } finally {
        busy := false
    }
}
