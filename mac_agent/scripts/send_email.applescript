on run argv
	set theTo to item 1 of argv
	set theSubject to item 2 of argv
	set theBody to item 3 of argv
	tell application "Mail"
		set newMessage to make new outgoing message with properties {subject:theSubject, content:theBody, visible:false}
		tell newMessage
			make new to recipient at end of to recipients with properties {address:theTo}
		end tell
		send newMessage
	end tell
end run
