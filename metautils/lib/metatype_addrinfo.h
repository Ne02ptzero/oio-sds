/*
OpenIO SDS metautils
Copyright (C) 2014 Worldine, original work as part of Redcurrant
Copyright (C) 2015 OpenIO, modified as part of OpenIO Software Defined Storage

This library is free software; you can redistribute it and/or
modify it under the terms of the GNU Lesser General Public
License as published by the Free Software Foundation; either
version 3.0 of the License, or (at your option) any later version.

This library is distributed in the hope that it will be useful,
but WITHOUT ANY WARRANTY; without even the implied warranty of
MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the GNU
Lesser General Public License for more details.

You should have received a copy of the GNU Lesser General Public
License along with this library.
*/

#ifndef OIO_SDS__metautils__lib__metatype_addrinfo_h
# define OIO_SDS__metautils__lib__metatype_addrinfo_h 1

#include <glib/gtypes.h>

gboolean addr_info_equal(gconstpointer a, gconstpointer b);
gint addr_info_compare(gconstpointer a, gconstpointer b);
guint addr_info_hash(gconstpointer k);

/* convert a service string (as returned by meta1) into an addr_info */
addr_info_t * addr_info_from_service_str(const gchar *service);

#define addr_info_clean  g_free0
#define addr_info_gclean g_free1

#endif /*OIO_SDS__metautils__lib__metatype_addrinfo_h*/
