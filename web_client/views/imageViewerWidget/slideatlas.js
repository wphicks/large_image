import { restRequest } from 'girder/rest';
import { staticRoot } from 'girder/rest';
import ImageViewerWidget from './base';
import Backbone from 'backbone';
import { splitRoute, parseQueryString } from 'girder/misc';
//import router from '../../router';



var SlideAtlasImageViewerWidget = ImageViewerWidget.extend({
    initialize: function (settings) {
        if (!$('head #large_image-slideatlas-css').length) {
            $('head').prepend(
                $('<link>', {
                    id: 'large_image-slideatlas-css',
                    rel: 'stylesheet',
                    href: staticRoot + '/built/plugins/large_image/extra/slideatlas/sa.css'
                })
            );
        }

        $.when(
            ImageViewerWidget.prototype.initialize.call(this, settings),
            $.ajax({ // like $.getScript, but allow caching
                url: staticRoot + '/built/plugins/large_image/extra/slideatlas/sa-all.max.js',
                dataType: 'script',
                cache: true
            }))
            .done(() => this.render());
    },

    uploadImage: function (dataUrl, girderFileId) {
      // We might try to get binary from the canvas.
      //canvas.toBlob(onsuccess);

      var BASE64_MARKER = ';base64,';
      var base64Index = dataUrl.indexOf(BASE64_MARKER) + BASE64_MARKER.length;
      var base64 = dataUrl.substring(base64Index);
      var raw = window.atob(base64);
      var rawLength = raw.length;
      var array = new Uint8Array(new ArrayBuffer(rawLength));
      for(var i = 0; i < rawLength; i++) {
        array[i] = raw.charCodeAt(i);
      }

      //var size = dataUrl.length;
      var size = rawLength;
      var url = 'file?parentType=item&parentId=5990fc973f24e54cbd1469b9&name=junk.png&size='+size.toString();
      // size=,linkUrl=      
      // Content-Transfer-Encoding: "BASE64",
      //contentTransferEncoding: "base64",
      restRequest({url: url,
                   contentType: 'image/png',
                   processData: false,
                   data: array,
                   type: 'POST',
                  })
        .done(function (d) {
          var uploadId = d._id;
        });
    },
  
    render: function () {
        // render can get clled multiple times
        if (this.viewer) {
            return this;
        }

        // If script or metadata isn't loaded, then abort
        if (!window.SA || !this.tileWidth || !this.tileHeight || this.deleted) {
            return this;
        }

        if (this.viewer) {
            // don't rerender the viewer
            return this;
        }

        // TODO: if a viewer already exists, do we render again?
        // SlideAtlas bundles its own version of jQuery, which should attach itself to "window.$" when it's sourced
        // The "this.$el" still uses the Girder version of jQuery, which will not have "saViewer" registered on it.
        var tileSource = {
            height: this.sizeY,
            width: this.sizeX,
            tileWidth: this.tileWidth,
            tileHeight: this.tileHeight,
            minLevel: 0,
            maxLevel: this.levels - 1,
            units: 'mm',
            spacing: [this.mm_x, this.mm_y],
            getTileUrl: (level, x, y, z) => {
                // Drop the "z" argument
                return this._getTileUrl(level, x, y);
            }
        };
        if (!this.mm_x) {
            // tileSource.units = 'pixels';
            tileSource.spacing = [1, 1];
        }

        window.SA.SAViewer(window.$(this.el), {
            zoomWidget: true,
            drawWidget: true,
            prefixUrl: staticRoot + '/built/plugins/large_image/extra/slideatlas/img/',
            tileSource: tileSource
        });
        this.viewer = this.el.saViewer;

        this.girderGui = new window.SAM.GirderAnnotationPanel(this.viewer, this.itemId);
        $(this.el).css({position: 'relative'});
        window.SA.SAFullScreenButton($(this.el))
          .css({'position': 'absolute', 'left': '2px', 'top': '2px'});
        SA.GirderView = this;
      
        // Set the view from the URL if bounds are specified.
        var curRoute = Backbone.history.fragment,
          routeParts = splitRoute(curRoute),
          queryString = parseQueryString(routeParts.name);

        if (queryString.bounds) {
          var rot = 0
          if (queryString.rotate) {
            rot = parseInt(queryString.rotate);
          }
          var bds = queryString.bounds.split(',')
          var x0 = parseInt(bds[0])
          var y0 = parseInt(bds[1])
          var x1 = parseInt(bds[2])
          var y1 = parseInt(bds[3])
          this.viewer.SetCamera([(x0+x1)*0.5, (y0+y1)*0.5], rot, (y1-y0));
        }
      
        this.trigger('g:imageRendered', this);

        return this;
    },

    destroy: function () {
        if (this.viewer) {
            window.$(this.el).saViewer('destroy');
            this.viewer = null;
        }
        this.deleted = true;
        ImageViewerWidget.prototype.destroy.call(this);
    }
});

export default SlideAtlasImageViewerWidget;
