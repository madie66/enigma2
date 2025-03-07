#ifndef __lib_listbox_h
#define __lib_listbox_h

#include <lib/gui/ewidget.h>
#include <connection.h>

class eListbox;
class eSlider;

class iListboxContent: public iObject
{
public:
	virtual ~iListboxContent()=0;

		/* indices go from 0 to size().
		   the end is reached when the cursor is on size(),
		   i.e. one after the last entry (this mimics
		   stl behavior)

		   cursors never invalidate - they can become invalid
		   when stuff is removed. Cursors will always try
		   to stay on the same data, however when the current
		   item is removed, this won't work. you'll be notified
		   anyway. */
#ifndef SWIG
protected:
	iListboxContent();
	friend class eListbox;
	virtual void updateClip(gRegion &){ };
	virtual void resetClip(){ };
	virtual void cursorHome()=0;
	virtual void cursorEnd()=0;
	virtual int cursorMove(int count=1)=0;
	virtual int cursorValid()=0;
	virtual int cursorSet(int n)=0;
	virtual int cursorGet()=0;

	virtual void cursorSave()=0;
	virtual void cursorRestore()=0;
	virtual void cursorSaveTop(int n)=0;
	virtual int cursorRestoreTop()=0;

	virtual int size()=0;

	virtual int currentCursorSelectable();

	void setListbox(eListbox *lb);

	// void setOutputDevice ? (for allocating colors, ...) .. requires some work, though
	virtual void setSize(const eSize &size)=0;

		/* the following functions always refer to the selected item */
	virtual void paint(gPainter &painter, eWindowStyle &style, const ePoint &offset, int selected)=0;

	virtual int getItemHeight()=0;

	eListbox *m_listbox;
#endif
};

#ifndef SWIG
struct eListboxStyle
{
	ePtr<gPixmap> m_background, m_selection;
	int m_transparent_background;
	gRGB m_background_color, m_background_color_selected,
	m_foreground_color, m_foreground_color_selected, m_border_color, m_scollbarborder_color, m_scrollbarforeground_color, m_scrollbarbackground_color;
	int m_background_color_set, m_foreground_color_set, m_background_color_selected_set, m_foreground_color_selected_set, m_scrollbarforeground_color_set, m_scrollbarbackground_color_set, m_scollbarborder_color_set, m_scrollbarborder_width_set;
		/*
			{m_transparent_background m_background_color_set m_background}
			{0 0 0} use global background color
			{0 1 x} use background color
			{0 0 p} use background picture
			{1 x 0} use transparent background
			{1 x p} use transparent background picture
		*/

	enum
	{
		alignLeft,
		alignTop=alignLeft,
		alignCenter,
		alignRight,
		alignBottom=alignRight,
		alignBlock
	};
	int m_valign, m_halign, m_border_size, m_scrollbarborder_width;
	ePtr<gFont> m_font, m_valuefont;
	ePoint m_text_offset;
	bool m_use_vti_workaround;
};
#endif

class eListbox: public eWidget
{
	void updateScrollBar();
public:
	eListbox(eWidget *parent);
	~eListbox();

	PSignal0<void> selectionChanged;

	enum {
		showOnDemand,
		showAlways,
		showNever,
		showLeftOnDemand,
		showLeftAlways
	};

	enum {
		byPage,
		byLine
	};

	enum {
		DefaultScrollBarWidth = 10,
		DefaultScrollBarOffset = 5,
		DefaultScrollBarBorderWidth = 1,
		DefaultScrollBarScroll = eListbox::byPage,
		DefaultScrollBarMode = eListbox::showNever,
		DefaultWrapAround = true
	};

	void setScrollbarScroll(int scroll);
	void setScrollbarMode(int mode);
	void setWrapAround(bool);

	void setContent(iListboxContent *content);

	void allowNativeKeys(bool allow);
	void enableAutoNavigation(bool allow) { allowNativeKeys(allow); }

/*	enum Movement {
		moveUp,
		moveDown,
		moveTop,
		moveEnd,
		justCheck
	}; */

	int getCurrentIndex();
	void moveSelection(long how);
	void moveSelectionTo(int index);
	void moveToEnd();
	bool atBegin();
	bool atEnd();

	enum ListboxActions {
		moveUp,
		moveDown,
		moveTop,
		moveEnd,
		pageUp,
		pageDown,
		justCheck,
		refresh,
		moveLeft,
		moveRight,
		moveFirst = moveTop,
		moveBottom = moveEnd,
		moveLast = moveEnd,
		movePageUp = pageUp,
		movePageDown = pageDown
	};

	void setItemHeight(int h);
	void setSelectionEnable(int en);

	void setBackgroundColor(gRGB &col);
	void setBackgroundColorSelected(gRGB &col);
	void setForegroundColor(gRGB &col);
	void setForegroundColorSelected(gRGB &col);
	void setBorderColor(const gRGB &col);
	void setBorderWidth(int size);
	void setBackgroundPixmap(ePtr<gPixmap> &pixmap);
	void setSelectionPixmap(ePtr<gPixmap> &pixmap);

	void setScrollbarForegroundPixmap(ePtr<gPixmap> &pm);
	void setScrollbarBackgroundPixmap(ePtr<gPixmap> &pm);
	void setScrollbarBorderWidth(int width);
	void setScrollbarWidth(int size);
	void setScrollbarOffset(int size);

	void setFont(gFont *font);
	void setEntryFont(gFont *font);
	void setValueFont(gFont *font);
	void setVAlign(int align);
	void setHAlign(int align);
	void setTextOffset(const ePoint &textoffset);
	void setUseVTIWorkaround(void);

	void setScrollbarBorderColor(const gRGB &col);
	void setScrollbarForegroundColor(gRGB &col);
	void setScrollbarBackgroundColor(gRGB &col);

	static void setDefaultScrollbarStyle(int width, int offset, int borderwidth, int scroll, int mode, bool enablewraparound) { 
			defaultScrollBarWidth = width; 
			defaultScrollBarOffset = offset; 
			defaultScrollBarBorderWidth = borderwidth; 
			defaultScrollBarScroll = scroll; 
			defaultWrapAround = enablewraparound;
			defaultScrollBarMode = mode;
		}

	bool getWrapAround() { return m_enabled_wrap_around; }
	int getScrollbarScroll() { return m_scrollbar_scroll; }
	int getScrollbarMode() { return m_scrollbar_mode; }
	int getScrollbarWidth() { return m_scrollbar_width; }
	int getScrollbarOffset() { return m_scrollbar_offset; }
	int getScrollbarBorderWidth() { return m_scrollbar_border_width; }
	int getItemHeight() { return m_itemheight; }
	bool getSelectionEnable() {return m_selection_enabled; }
	gFont* getFont() {return m_style.m_font; }
	gFont* getEntryFont() {return m_style.m_font; }
	gFont* getValueFont() {return m_style.m_valuefont; }


#ifndef SWIG
	struct eListboxStyle *getLocalStyle(void);

		/* entryAdded: an entry was added *before* the given index. it's index is the given number. */
	void entryAdded(int index);
		/* entryRemoved: an entry with the given index was removed. */
	void entryRemoved(int index);
		/* entryChanged: the entry with the given index was changed and should be redrawn. */
	void entryChanged(int index);
		/* the complete list changed. you should not attemp to keep the current index. */
	void entryReset(bool cursorHome=true);

	int getEntryTop();
	void invalidate(const gRegion &region = gRegion::invalidRegion());

protected:
	int event(int event, void *data=0, void *data2=0);
	void recalcSize();

private:
	static int defaultScrollBarWidth;
	static int defaultScrollBarOffset;
	static int defaultScrollBarBorderWidth;
	static int defaultScrollBarScroll;
	static int defaultScrollBarMode;
	static bool defaultWrapAround;

	int m_scrollbar_mode, m_prev_scrollbar_page, m_scrollbar_scroll;
	bool m_content_changed;
	bool m_enabled_wrap_around;

	int m_scrollbar_width;
	int m_scrollbar_offset;
	int m_scrollbar_border_width;
	int m_top, m_selected;
	int m_itemheight;
	int m_items_per_page;
	int m_selection_enabled;

	bool m_native_keys_bound;

	ePtr<iListboxContent> m_content;
	eSlider *m_scrollbar;
	eListboxStyle m_style;
	ePtr<gPixmap> m_scrollbarpixmap, m_scrollbarbackgroundpixmap;
#ifdef USE_LIBVUGLES2
	long m_dir;
#endif
#endif
};

#endif
