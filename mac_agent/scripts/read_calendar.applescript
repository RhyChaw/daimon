on run argv
	set todayStart to current date
	set time of todayStart to 0
	set tomorrowStart to todayStart + (1 * days)
	set eventLines to {}
	tell application "Calendar"
		repeat with cal in calendars
			set dayEvents to (every event of cal whose start date < tomorrowStart and end date > todayStart)
			repeat with e in dayEvents
				set s to start date of e
				set en to end date of e
				set isAllDay to allday event of e
				set titleText to my cleanField(summary of e)
				set locText to ""
				try
					set locText to my cleanField(location of e)
				end try
				set end of eventLines to my formatTs(s) & tab & my formatTs(en) & tab & isAllDay & tab & titleText & tab & locText
			end repeat
		end repeat
	end tell
	if (count of eventLines) is 0 then
		return ""
	end if
	set AppleScript's text item delimiters to linefeed
	return eventLines as text
end run

on formatTs(d)
	set y to year of d
	set m to my pad2((month of d) as integer)
	set dy to my pad2(day of d)
	set h to my pad2(hours of d)
	set mi to my pad2(minutes of d)
	return (y as text) & "-" & m & "-" & dy & " " & h & ":" & mi
end formatTs

on pad2(n)
	if n < 10 then
		return "0" & n
	end if
	return n as text
end pad2

on cleanField(t)
	if t is missing value then return ""
	set t to t as text
	set t to my replaceText(return, " ", t)
	set t to my replaceText(tab, " ", t)
	return t
end cleanField

on replaceText(find, repl, t)
	set AppleScript's text item delimiters to find
	set parts to text items of t
	set AppleScript's text item delimiters to repl
	return parts as text
end replaceText
