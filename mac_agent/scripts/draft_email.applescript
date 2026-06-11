on run argv
	set theTo to item 1 of argv
	set theSubject to item 2 of argv
	set theBody to item 3 of argv
	tell application "Mail"
		set newMessage to make new outgoing message with properties {subject:theSubject, content:theBody, visible:true}
		tell newMessage
			make new to recipient at end of to recipients with properties {address:theTo}
		end tell
		activate
	end tell
end run
