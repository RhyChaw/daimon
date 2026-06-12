on run argv
	if (count of argv) < 1 then
		error "mode required: search or recent"
	end if
	set playMode to item 1 of argv
	set trackQuery to ""
	if playMode is "search" then
		if (count of argv) < 2 then
			error "search mode requires a query"
		end if
		set trackQuery to item 2 of argv
		if trackQuery is missing value or trackQuery is "" then
			error "query required"
		end if
	else if playMode is not "recent" then
		error "unknown mode: " & playMode
	end if

	my focusSpotify()
	my openSearch()
	if playMode is "search" then
		my typeInSearch(trackQuery)
		my pressDown(5)
	else
		delay 0.5
		my pressDown(1)
	end if
	my pressReturn()
end run

on focusSpotify()
	tell application "Spotify"
		launch
		activate
	end tell
	delay 2.5
	repeat 30 times
		tell application "System Events"
			if (name of first application process whose frontmost is true) is "Spotify" then
				return
			end if
			if exists process "Spotify" then
				tell process "Spotify" to set frontmost to true
			end if
		end tell
		delay 0.2
	end repeat
	error "Could not focus Spotify — grant Daimon Accessibility in System Settings"
end focusSpotify

on openSearch()
	delay 0.6
	tell application "System Events"
		tell process "Spotify"
			set frontmost to true
			delay 0.3
			keystroke "l" using command down
		end tell
	end tell
	delay 1.2
end openSearch

on typeInSearch(trackQuery)
	tell application "System Events"
		tell process "Spotify"
			keystroke "a" using command down
			delay 0.15
			keystroke trackQuery
		end tell
	end tell
	delay 1.8
end typeInSearch

on pressDown(n)
	repeat n times
		tell application "System Events"
			tell process "Spotify"
				key code 125
			end tell
		end tell
		delay 0.2
	end repeat
end pressDown

on pressReturn()
	delay 0.2
	tell application "System Events"
		tell process "Spotify"
			key code 36
		end tell
	end tell
end pressReturn
