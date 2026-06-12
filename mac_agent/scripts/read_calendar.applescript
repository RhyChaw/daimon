on run argv
	set todayStart to current date
	set time of todayStart to 0
	set tomorrowStart to todayStart + (1 * days)
	set recurLookback to todayStart - (45 * days)
	set eventLines to {}
	tell application "Calendar"
		launch
		repeat with cal in calendars
			set dayEvents to (every event of cal whose start date < tomorrowStart and end date > todayStart)
			repeat with ev in dayEvents
				set s to start date of ev
				set endTime to end date of ev
				set dayFlag to allday event of ev
				set titleText to my cleanField(summary of ev)
				set locText to ""
				try
					set locText to my cleanField(location of ev)
				end try
				set end of eventLines to my formatTs(s) & tab & my formatTs(endTime) & tab & dayFlag & tab & titleText & tab & locText
			end repeat
			repeat with ev in (every event of cal whose start date is greater than or equal to recurLookback)
				try
					set r to recurrence of ev
					if r is not missing value then
						set rText to r as text
						set dowCode to my weekdayCode(todayStart)
						set occurs to false
						if rText contains "FREQ=DAILY" then
							set occurs to true
						else if rText contains "FREQ=WEEKLY" then
							if rText contains "BYDAY=" then
								set occurs to my bydayIncludes(rText, dowCode)
							else
								set occurs to (weekday of (start date of ev)) is (weekday of todayStart)
							end if
						end if
						if occurs then
							set ms to start date of ev
							set masterEnd to end date of ev
							set dur to (masterEnd - ms)
							set s to todayStart
							set time of s to (time of ms)
							set endTime to s + dur
							set dayFlag to allday event of ev
							set titleText to my cleanField(summary of ev)
							set locText to ""
							try
								set locText to my cleanField(location of ev)
							end try
							set end of eventLines to my formatTs(s) & tab & my formatTs(endTime) & tab & dayFlag & tab & titleText & tab & locText
						end if
					end if
				end try
			end repeat
		end repeat
	end tell
	if (count of eventLines) is 0 then
		return ""
	end if
	set AppleScript's text item delimiters to linefeed
	return eventLines as text
end run

on bydayIncludes(rText, dowCode)
	set AppleScript's text item delimiters to "BYDAY="
	set parts to text items of rText
	if (count of parts) < 2 then return false
	set daysPart to item 2 of parts
	set AppleScript's text item delimiters to ";"
	set dayToken to item 1 of (text items of daysPart)
	set AppleScript's text item delimiters to ","
	repeat with d in text items of dayToken
		if (d as text) is dowCode then return true
	end repeat
	return false
end bydayIncludes

on weekdayCode(d)
	set w to weekday of d
	if w is Sunday then return "SU"
	if w is Monday then return "MO"
	if w is Tuesday then return "TU"
	if w is Wednesday then return "WE"
	if w is Thursday then return "TH"
	if w is Friday then return "FR"
	if w is Saturday then return "SA"
	return ""
end weekdayCode

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
