# Edit Lightroom photos through conversation

[中文](USAGE.md) | English

After installation, open a Catalog in Lightroom Classic, select the photo to edit, and return to an Agent client already connected to ToneRelay. For installation help, see the [installation guide](INSTALL.en.md).

## View the current photo and others in the same folder

> Start editing in Lightroom and show the currently selected photo.

> Show me 10 previews from the current photo's folder, with filenames.

You can continue browsing the folder in batches on the next page. The current photo comes from Lightroom's selection at the time of the request; after switching photos, ask the Agent to read it again.

## Discuss the goal, then review a first result

> Keep it natural, open up the shadows a little, and soften the highlights. Show me a first version.

After reviewing the result, point out specific areas and impressions, such as “the person is still a little dark,” “the sky is too cyan,” or “the shadows are too flat now.” You can also ask to see an existing version or return to an earlier version you liked.

## Color-grade using a reference image

Send a reference image in the conversation, then describe the features that matter most:

> Use this image's warm/cool balance and shadow depth as a reference, while preserving the person and scene in the current photo.

Reference goals can include color, tone, atmosphere, or relationships between local areas. Different scenes and lighting need individual judgment; fine-tune after reviewing the actual result.

## Adjust people and local light

> Keep the background unchanged for now, restore the person's exposure, and check whether the edges look natural.

The Agent can combine the Lightroom local adjustments currently available to make the changes. The edges and coverage of a subject mask still need visual review. If you want to see the mask and the final image, ask explicitly.

## Organize a set of photos

> Review this set and suggest star ratings and pick flags first. Write them back only after I confirm, and preserve existing marks.

Specify which photos to process and the criteria to use. Star ratings, flags, and color labels are organization results that are written to the Lightroom Catalog. To undo them, tell the Agent which marking operation to undo; fields changed afterward are handled according to their actual current state.

## Find styles and effects

Choose a style or effect pack in the [ToneRelay gallery](https://reefruan.github.io/tonerelay-portal/?lang=en#gallery), then give the relevant content and photo goal to the Agent. Packs provide visual context; the Agent decides specific parameters for the individual photo and applies them through Lightroom.

## View status and configuration

> Show the ToneRelay configuration, including preview image size and current connection status.

> Set the longest edge of conversation previews to 1024.

You can query configuration without a selected photo or when the connection is blocked. Image budgets and final photo export are separate settings. Before making a change, specify whether it concerns conversation previews or subsequent working renders.

Regular Develop versions are saved as virtual copies. Writing directly to RAW sidecars, importing, or performing other file operations requires an explicitly specified target. If an operation times out, the connection drops, or the result is uncertain, check the actual state before deciding what to do next.

[Back to introduction](README.en.md) · [Installation and recovery](INSTALL.en.md) · [See actual results](https://reefruan.github.io/tonerelay-portal/?lang=en#experience)
